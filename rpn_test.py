import os
import math

from core.detection_module import DetModule
from core.detection_input import Loader
from utils.load_model import load_checkpoint
from utils.patch_config import patch_config_as_nothrow

from six.moves import reduce
from six.moves.queue import Queue
from threading import Thread
import argparse
import importlib
import mxnet as mx
import numpy as np
import six.moves.cPickle as pkl


def parse_args():
    parser = argparse.ArgumentParser(description='Test Detection')
    # general
    parser.add_argument('--config', help='config file path', type=str)
    args = parser.parse_args()

    config = importlib.import_module(args.config.replace('.py', '').replace('/', '.'))
    return config


if __name__ == "__main__":
    os.environ["MXNET_CUDNN_AUTOTUNE_DEFAULT"] = "0"

    config = parse_args()

    pGen, pKv, pRpn, pRoi, pBbox, pDataset, pModel, pOpt, pTest, \
    transform, data_name, label_name, metric_list = config.get_config(is_train=False)
    pGen = patch_config_as_nothrow(pGen)
    pKv = patch_config_as_nothrow(pKv)
    pRpn = patch_config_as_nothrow(pRpn)
    pRoi = patch_config_as_nothrow(pRoi)
    pBbox = patch_config_as_nothrow(pBbox)
    pDataset = patch_config_as_nothrow(pDataset)
    pModel = patch_config_as_nothrow(pModel)
    pOpt = patch_config_as_nothrow(pOpt)
    pTest = patch_config_as_nothrow(pTest)

    sym = pModel.rpn_test_symbol
    sym.save(pTest.model.prefix + "_test.json")

    image_sets = pDataset.image_set
    roidbs_all = [pkl.load(open("data/cache/{}.roidb".format(i), "rb"), encoding="latin1") for i in image_sets]
    roidbs_all = reduce(lambda x, y: x + y, roidbs_all)

    from pycocotools.coco import COCO
    from pycocotools.cocoeval import COCOeval
    coco = COCO(pTest.coco.annotation)

    data_queue = Queue(100)
    result_queue = Queue()

    execs = []
    workers = []
    coco_result = []
    split_size = 1000

    for index_split in range(int(math.ceil(len(roidbs_all) / split_size))):
        print("evaluating [%d, %d)" % (index_split * split_size, (index_split + 1) * split_size))
        roidb = roidbs_all[index_split * split_size:(index_split + 1) * split_size]
        roidb = pTest.process_roidb(roidb)
        for i, x in enumerate(roidb):
            x["rec_id"] = i

        loader = Loader(roidb=roidb,
                        transform=transform,
                        data_name=data_name,
                        label_name=label_name,
                        batch_size=1,
                        shuffle=False,
                        num_worker=4,
                        num_collector=2,
                        worker_queue_depth=2,
                        collector_queue_depth=2,
                        kv=None)

        print("total number of images: {}".format(loader.total_record))

        data_names = [k[0] for k in loader.provide_data]

        if index_split == 0:
            for i in pKv.gpus:
                ctx = mx.gpu(i)
                arg_params, aux_params = load_checkpoint(pTest.model.prefix, pTest.model.epoch)
                mod = DetModule(sym, data_names=data_names, context=ctx)
                mod.bind(data_shapes=loader.provide_data, for_training=False)
                mod.set_params(arg_params, aux_params, allow_extra=False)
                execs.append(mod)

        all_outputs = []

        if index_split == 0:
            def eval_worker(exe, data_queue, result_queue):
                while True:
                    batch = data_queue.get()
                    exe.forward(batch, is_train=False)
                    out = [x.asnumpy() for x in exe.get_outputs()]
                    result_queue.put(out)
            for exe in execs:
                workers.append(Thread(target=eval_worker, args=(exe, data_queue, result_queue)))
            for w in workers:
                w.daemon = True
                w.start()

        import time
        t1_s = time.time()

        def data_enqueue(loader, data_queue):
            for batch in loader:
                data_queue.put(batch)
        enqueue_worker = Thread(target=data_enqueue, args=(loader, data_queue))
        enqueue_worker.daemon = True
        enqueue_worker.start()

        for _ in range(loader.total_record):
            r = result_queue.get()

            rid, id, info, box, score = r
            rid, id, info, box, score = rid.squeeze(), id.squeeze(), info.squeeze(), box.squeeze(), score.squeeze()
            # TODO: POTENTIAL BUG, id or rid overflows float32(int23, 16.7M)
            id = np.asscalar(id)
            rid = np.asscalar(rid)

            scale = info[2]  # h_raw, w_raw, scale
            box = box / scale  # scale to original image scale

            output_record = dict(
                rec_id=rid,
                im_id=id,
                im_info=info,
                bbox_xyxy=box,  # ndarray (n, class * 4) or (n, 4)
                cls_score=score   # ndarray (n, class)
            )

            all_outputs.append(output_record)

        t2_s = time.time()
        print("network uses: %.1f" % (t2_s - t1_s))

        # let user process all_outputs
        if pTest.process_rpn_output is not None:
            if callable(pTest.process_rpn_output):
                pTest.process_rpn_output = [pTest.process_rpn_output]
            for callback in pTest.process_rpn_output:
                all_outputs = callback(all_outputs, roidb)

        # aggregate results for ensemble and multi-scale test
        output_dict = {}
        for rec in all_outputs:
            im_id = rec["im_id"]
            if im_id not in output_dict:
                output_dict[im_id] = dict(
                    bbox_xyxy=[rec["bbox_xyxy"]],
                    cls_score=[rec["cls_score"]]
                )
            else:
                output_dict[im_id]["bbox_xyxy"].append(rec["bbox_xyxy"])
                output_dict[im_id]["cls_score"].append(rec["cls_score"])

        for k in output_dict:
            if len(output_dict[k]["bbox_xyxy"]) > 1:
                output_dict[k]["bbox_xyxy"] = np.concatenate(output_dict[k]["bbox_xyxy"])
            else:
                output_dict[k]["bbox_xyxy"] = output_dict[k]["bbox_xyxy"][0]

            if len(output_dict[k]["cls_score"]) > 1:
                output_dict[k]["cls_score"] = np.concatenate(output_dict[k]["cls_score"])
            else:
                output_dict[k]["cls_score"] = output_dict[k]["cls_score"][0]

        t3_s = time.time()
        print("aggregate uses: %.1f" % (t3_s - t2_s))

        for iid in output_dict:
            result = []
            det = output_dict[iid]["bbox_xyxy"]
            if det.shape[0] == 0:
                continue
            scores = output_dict[iid]["cls_score"]
            xs = det[:, 0]
            ys = det[:, 1]
            ws = det[:, 2] - xs + 1
            hs = det[:, 3] - ys + 1
            result += [
                {'image_id': int(iid),
                    'category_id': 1,
                    'bbox': [float(xs[k]), float(ys[k]), float(ws[k]), float(hs[k])],
                    'score': float(scores[k])}
                for k in range(det.shape[0])
            ]
            result = sorted(result, key=lambda x: x['score'])[-100:]
            coco_result += result

        t5_s = time.time()
        print("convert to coco format uses: %.1f" % (t5_s - t3_s))

    import json
    json.dump(coco_result,
              open("experiments/{}/{}_proposal_result.json".format(pGen.name, pDataset.image_set[0]), "w"),
              sort_keys=True, indent=2)

    coco_dt = coco.loadRes(coco_result)
    coco_eval = COCOeval(coco, coco_dt)
    coco_eval.params.iouType = "bbox"
    coco_eval.params.maxDets = [1, 10, 100]  # [100, 300, 1000]
    coco_eval.params.useCats = False
    coco_eval.evaluate()
    coco_eval.accumulate()
    coco_eval.summarize()

    t6_s = time.time()
    print("coco eval uses: %.1f" % (t6_s - t5_s))
