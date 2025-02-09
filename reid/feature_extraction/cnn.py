from __future__ import absolute_import

from collections import OrderedDict
import torch

from ..utils import to_torch


def extract_cnn_feature(model, inputs, modules=None, norm=False):
    model.eval()
    inputs = to_torch(inputs)

    with torch.no_grad():
        if modules is None:
            if not norm:
                outputs, _, _ = model(inputs)
            else:
                _, outputs, _ = model(inputs)
            outputs = outputs.data.cpu()
            return outputs
        # Register forward hook for each module
        outputs = OrderedDict()
        handles = []
        for m in modules:
            outputs[id(m)] = None
            def func(m, i, o): outputs[id(m)] = o.data.cpu()
            handles.append(m.register_forward_hook(func))
        model(inputs)
        for h in handles:
            h.remove()
        return list(outputs.values())
