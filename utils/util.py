"""
Exclude from autoreload
%aimport -util.utils
"""
import os
import sys
import json
import time
import pickle
import socket
import functools
from pathlib import Path
from datetime import datetime
import random
from itertools import repeat
from collections import OrderedDict

import numpy as np
import torch
import psutil
import msgpack
import humanize
import msgpack_numpy as msgpack_np
from PIL import Image

import utils.datastructures as datastructures

msgpack_np.patch()


def set_seeds(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def memory_summary():
    vmem = psutil.virtual_memory()
    msg = (
        f">>> Currently using {vmem.percent}% of system memory "
        f"{humanize.naturalsize(vmem.used)}/{humanize.naturalsize(vmem.available)}"
    )
    print(msg)


def flatten_dict(x, keysep="-"):
    flat_dict = {}
    for key, val in x.items():
        if isinstance(val, dict):
            flat_subdict = flatten_dict(val)
            flat_dict.update({f"{key}{keysep}{subkey}": subval
                              for subkey, subval in flat_subdict.items()})
        else:
            flat_dict.update({key: val})
    return flat_dict


def set_nested_key_val(key, val, target):
    """Use a prefix key (e.g. key1.key2.key3) to set a value in a nested dict"""
    # escape periods in keys
    key = key.replace("_.", "&&")
    subkeys = key.split(".")
    subkeys = [x.replace("&&", ".") for x in subkeys]

    nested = target
    print("subkeys", subkeys)
    for subkey in subkeys[:-1]:
        try:
            nested = nested.__getitem__(subkey)
        except:
            print(subkey)
            import ipdb; ipdb.set_trace()
    orig = nested[subkeys[-1]]
    if orig == "":
        if val == "":
            val = 0
        else:
            val = str(val)
    elif isinstance(orig, bool):
        if val.lower() in {"0", "False"}:
            val = False
        else:
            val = bool(val)
    elif isinstance(orig, list):
        if isinstance(val, str) and "," in val:
            val = val.split(",")
            # we use the convention that a trailing comma indicates a single item list
            if len(val) == 2 and val[1] == "":
                val.pop()
            if val and not orig:
                raise ValueError(f"Could not infer correct type from empty original list")
            else:
                val = [type(orig[0])(x) for x in val]
        assert isinstance(val, list), "Failed to pass a list where expected"
    elif isinstance(orig, int):
        val = int(val)
    elif isinstance(orig, float):
        val = float(val)
    elif isinstance(orig, str):
        val = str(val)
    else:
        print(f"unrecognised type: {type(val)}")
        import ipdb; ipdb.set_trace()
    nested[subkeys[-1]] = val



def expert_tensor_storage(experts, feat_aggregation):
    expert_storage = {"fixed": set(), "variable": set(), "flaky": set()}
    # fixed_sz_experts, variable_sz_experts, flaky_experts = set(), set(), set()
    for expert, config in feat_aggregation.items():
        if config["temporal"] in {"vlad"}:
            expert_storage["variable"].add(expert)
        elif config["temporal"] in {"avg", "max", "avg-max", "max-avg", "avg-max-ent",
                                    "max-avg-ent"}:
            expert_storage["fixed"].add(expert)
        else:
            raise ValueError(f"unknown temporal strategy: {config['temporal']}")
        # some "flaky" experts are only available for a fraction of videos - we need
        # to pass this information (in the form of indices) into the network for any
        # experts present in the current dataset
        if config.get("flaky", False):
            expert_storage["flaky"].add(expert)

    # we only allocate storage for experts used by the current dataset
    for key, value in expert_storage.items():
        expert_storage[key] = value.intersection(set(experts))
    return expert_storage


@functools.lru_cache(maxsize=64, typed=False)
def concat_features(feat_paths, axis):
    aggregates = [memcache(x) for x in feat_paths]
    tic = time.time()
    msg = "expected to concatenate datastructures of a single type"
    assert len(set(type(x) for x in aggregates)) == 1, msg
    if isinstance(aggregates[0], dict):
        keys = aggregates[0]  # for now, we assume that all aggregates share keys
        merged = {}
        for key in keys:
            merged[key] = np.concatenate([x[key] for x in aggregates], axis=axis)
    elif isinstance(aggregates[0], datastructures.ExpertStore):
        dims, stores = [], []
        keys = aggregates[0].keys
        for x in aggregates:
            dims.append(x.dim)
            stores.append(x.store)
            try:
                assert x.keys == keys, "all aggregates must share identical keys"
            except Exception as E:
                print(E)
                import ipdb; ipdb.set_trace()
        msg = "expected to concatenate ExpertStores with a common dimension"
        assert len(set(dims)) == 1, msg
        dim = dims[0]
        merged = datastructures.ExpertStore(keys, dim=dim)
        merged.store = np.concatenate(stores, axis=axis)
    else:
        raise ValueError(f"Unknown datastructure: {type(aggregates[0])}")
    # Force memory clearance
    for aggregate in aggregates:
        del aggregate
    print("done in {:.3f}s".format(time.time() - tic))
    return merged


@functools.lru_cache(maxsize=64, typed=False)
def memcache(path):
    suffix = Path(path).suffix
    print(f"loading features >>>", end=" ")
    tic = time.time()
    if suffix in {".pkl", ".pickle"}:
        res = pickle_loader(path)
    elif suffix == ".npy":
        res = np_loader(path)
    elif suffix == ".mp":
        res = msgpack_loader(path)
    else:
        raise ValueError(f"unknown suffix: {suffix} for path {path}")
    print(f"[Total: {time.time() - tic:.1f}s] ({socket.gethostname() + ':' + str(path)})")
    return res


def ensure_dir(dirname):
    dirname = Path(dirname)
    if not dirname.is_dir():
        dirname.mkdir(parents=True, exist_ok=False)


def read_json(fname):
    with fname.open('rt') as handle:
        return json.load(handle, object_hook=OrderedDict)


def path2str(x):
    """Recursively convert pathlib objects to strings to enable serialization"""
    for key, val in x.items():
        if isinstance(val, dict):
            path2str(val)
        elif isinstance(val, Path):
            x[key] = str(val)


def write_json(content, fname, paths2strs=False):
    if paths2strs:
        path2str(content)
    with fname.open('wt') as handle:
        json.dump(content, handle, indent=4, sort_keys=False)


def inf_loop(data_loader):
    ''' wrapper function for endless data loader. '''
    for loader in repeat(data_loader):
        yield from loader


def pickle_loader(pkl_path):
    tic = time.time()
    with open(pkl_path, "rb") as f:
        buffer = f.read()
        print(f"[I/O: {time.time() - tic:.1f}s]", end=" ")
        tic = time.time()
        # patch the missing datastructures import (this was moved after refactoring)
        sys.modules["datastructures"] = datastructures
        data = pickle.loads(buffer, encoding="latin1")
        print(f"[deserialisation: {time.time() - tic:.1f}s]", end=" ")
    return data


def msgpack_loader(mp_path):
    """Msgpack provides a faster serialisation routine than pickle, so is preferable
    for loading and deserialising large feature sets from disk."""
    tic = time.time()
    with open(mp_path, "rb") as f:
        buffer = f.read()
        print(f"[I/O: {time.time() - tic:.1f}s]", end=" ")
        tic = time.time()
        ## super danger! yang :utf-8 ==> latin
        data = msgpack_np.unpackb(buffer, object_hook=msgpack_np.decode, encoding="latin") 
        print(f"[deserialisation: {time.time() - tic:.1f}s]", end=" ")
    return data


def np_loader(np_path, l2norm=False):
    with open(np_path, "rb") as f:
        data = np.load(f, encoding="latin1", allow_pickle=True)
    if isinstance(data, np.ndarray) and data.size == 1:
        data = data[()]  # handle numpy dict storage convnetion
    if l2norm:
        print("L2 normalizing features")
        if isinstance(data, dict):
            for key in data:
                feats_ = data[key]
                feats_ = feats_ / max(np.linalg.norm(feats_), 1E-6)
                data[key] = feats_
        elif data.ndim == 2:
            data_norm = np.linalg.norm(data, axis=1)
            data = data / np.maximum(data_norm.reshape(-1, 1), 1E-6)
        else:
            raise ValueError("unexpected data format {}".format(type(data)))
    return data


class HashableDict(dict):
    def __hash__(self):
        return hash(frozenset(self))


class HashableOrderedDict(dict):
    def __hash__(self):
        return hash(frozenset(self))


def compute_trn_config(config, logger=None):
    trn_config = {}
    feat_agg = config["data_loader"]["args"]["feat_aggregation"]
    for static_expert in feat_agg.keys():
        if static_expert in feat_agg:
            if "trn_seg" in feat_agg[static_expert].keys():
                trn_config[static_expert] = feat_agg[static_expert]["trn_seg"]
    return trn_config


def compute_dims(config, logger=None):
    if logger is None:
        logger = config.get_logger('utils')

    experts = config["experts"]
    # TODO(Samuel): clean up the logic since it's a little convoluted
    ordered = sorted(config["experts"]["modalities"])

    if experts["drop_feats"]:
        to_drop = experts["drop_feats"].split(",")
        logger.info(f"dropping: {to_drop}")
        ordered = [x for x in ordered if x not in to_drop]

    feat_agg = config["data_loader"]["args"]["feat_aggregation"]
    dims = []
    arch_args = config["arch"]["args"]
    vlad_clusters = arch_args["vlad_clusters"]
    msg = f"It is not valid to use both the `use_ce` and `mimic_ce_dims` options"
    assert not (arch_args["use_ce"] and arch_args.get("mimic_ce_dims", False)), msg
    for expert in ordered:
        if expert == "face":
            in_dim, out_dim = experts["face_dim"], experts["face_dim"]
        elif expert == "audio":
            in_dim, out_dim = 128 * vlad_clusters["audio"], 128
        elif expert == "speech":
            in_dim, out_dim = 300 * vlad_clusters["speech"], 300
        elif expert == "ocr":
            in_dim, out_dim = 300 * vlad_clusters["ocr"], 300
        elif expert == "detection":
            # allow for avg pooling
            det_clusters = arch_args["vlad_clusters"].get("detection", 1)
            in_dim, out_dim = 1541 * det_clusters, 1541
        elif expert == "detection-sem":
            if config["data_loader"]["args"].get("spatial_feats", False):
                base = 300 + 16
            else:
                base = 300 + 5
            det_clusters = arch_args["vlad_clusters"].get("detection-sem", 1)
            in_dim, out_dim = base * det_clusters, base
        elif expert == "openpose":
            base = 54
            det_clusters = arch_args["vlad_clusters"].get("openpose", 1)
            in_dim, out_dim = base * det_clusters, base
        else:
            common_dim = feat_agg[expert]["feat_dims"][feat_agg[expert]["type"]]
            # account for aggregation of multilpe forms (e.g. avg + max pooling)
            common_dim = common_dim * len(feat_agg[expert]["temporal"].split("-"))
            in_dim, out_dim = common_dim, common_dim

        # For the CE architecture, we need to project all features to a common
        # dimensionality
        if arch_args["use_ce"] or arch_args.get("mimic_ce_dims", False):
            out_dim = experts["ce_shared_dim"]

        dims.append((expert, (in_dim, out_dim)))
    expert_dims = OrderedDict(dims)

    if vlad_clusters["text"] == 0:
        msg = "vlad can only be disabled for text with single tokens"
        assert config["data_loader"]["args"]["max_tokens"]["text"] == 1, msg

    if config["experts"]["text_agg"] == "avg":
        msg = "averaging can only be performed with text using single tokens"
        assert config["arch"]["args"]["vlad_clusters"]["text"] == 0
        assert config["data_loader"]["args"]["max_tokens"]["text"] == 1

    # To remove the dependency of dataloader on the model architecture, we create a
    # second copy of the expert dimensions which accounts for the number of vlad
    # clusters
    raw_input_dims = OrderedDict()
    for expert, dim_pair in expert_dims.items():
        raw_dim = dim_pair[0]
        if expert in {"audio", "speech", "ocr", "detection", "detection-sem", "openpose",
                      "speech.mozilla.0"}:
            raw_dim = raw_dim // vlad_clusters.get(expert, 1)
        raw_input_dims[expert] = raw_dim

    return expert_dims, raw_input_dims


def ensure_tensor(x):
    if not isinstance(x, torch.Tensor):
        x = torch.from_numpy(x)
    return x


class Timer:
    def __init__(self):
        self.cache = datetime.now()

    def check(self):
        now = datetime.now()
        duration = now - self.cache
        self.cache = now
        return duration.total_seconds()

    def reset(self):
        self.cache = datetime.now()

def tensor2im(input_image, imtype=np.uint8):
    """"Converts a Tensor array into a numpy image array.

    Parameters:
        input_image (tensor) --  the input image tensor array
        imtype (type)        --  the desired type of the converted numpy array
    """
    if not isinstance(input_image, np.ndarray):
        if isinstance(input_image, torch.Tensor):  # get the data from a variable
            image_tensor = input_image.data
        else:
            return input_image
        # convert it into a numpy array
        image_numpy = image_tensor[0].cpu().float().numpy()  
        if image_numpy.shape[0] == 1:  # grayscale to RGB
            image_numpy = np.tile(image_numpy, (3, 1, 1))
        # post-processing: tranpose and scaling
        image_numpy = (np.transpose(image_numpy, (1, 2, 0)) + 1) / 2.0 * 255.0  
    else:  # if it is a numpy array, do nothing
        image_numpy = input_image
    return image_numpy.astype(imtype)


def save_image(image_numpy, image_path):
    """Save a numpy image to the disk

    Parameters:
        image_numpy (numpy array) -- input numpy array
        image_path (str)          -- the path of the image
    """
    image_pil = Image.fromarray(image_numpy)
    image_pil.save(image_path)


def print_numpy(x, val=True, shp=False):
    """Print the mean, min, max, median, std, and size of a numpy array

    Parameters:
        val (bool) -- if print the values of the numpy array
        shp (bool) -- if print the shape of the numpy array
    """
    x = x.astype(np.float64)
    if shp:
        print('shape,', x.shape)
    if val:
        x = x.flatten()
        print('mean = %3.3f, min = %3.3f, max = %3.3f, median = %3.3f, std=%3.3f' % (
            np.mean(x), np.min(x), np.max(x), np.median(x), np.std(x)))


def mkdirs(paths):
    """create empty directories if they don't exist

    Parameters:
        paths (str list) -- a list of directory paths
    """
    if isinstance(paths, list) and not isinstance(paths, str):
        for path in paths:
            mkdir(path)
    else:
        mkdir(paths)


def mkdir(path):
    """create a single empty directory if it didn't exist

    Parameters:
        path (str) -- a single directory path
    """
    if not os.path.exists(path):
        os.makedirs(path)
