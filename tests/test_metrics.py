import itertools

import numpy as np
import pytest
import torch
from torch.nn.utils import rnn

from pytorch_forecasting.data.encoders import TorchNormalizer
from pytorch_forecasting.metrics import (
    MAE,
    SMAPE,
    AggregationMetric,
    CompositeMetric,
    LogNormalDistributionLoss,
    NegativeBinomialDistributionLoss,
    NormalDistributionLoss,
)


def test_composite_metric():
    metric1 = SMAPE()
    metric2 = MAE()
    combined_metric = 1.0 * (0.3 * metric1 + 2.0 * metric2 + metric1)
    assert isinstance(combined_metric, CompositeMetric), "combined metric should be composite metric"

    # test repr()
    repr(combined_metric)

    # test results
    y = torch.normal(0, 1, (10, 20)).abs()
    y_pred = torch.normal(0, 1, (10, 20)).abs()

    res1 = metric1(y_pred, y)
    res2 = metric2(y_pred, y)
    combined_res = combined_metric(y_pred, y)

    assert torch.isclose(combined_res, res1 * 0.3 + res2 * 2.0 + res1)

    # test quantiles and prediction
    combined_metric.to_prediction(y_pred)
    combined_metric.to_quantiles(y_pred)


@pytest.mark.parametrize(
    "decoder_lengths,y",
    [
        (torch.tensor([1, 2], dtype=torch.long), torch.tensor([[0.0, 1.0], [5.0, 1.0]])),
        (2 * torch.ones(2, dtype=torch.long), torch.tensor([[0.0, 1.0], [5.0, 1.0]])),
        (2 * torch.ones(2, dtype=torch.long), torch.tensor([[[0.0, 1.0], [1.0, 1.0]], [[5.0, 1.0], [1.0, 2.0]]])),
    ],
)
def test_aggregation_metric(decoder_lengths, y):
    y_pred = torch.tensor([[0.0, 2.0], [4.0, 3.0]])
    if (decoder_lengths != y_pred.size(-1)).any():
        y_packed = rnn.pack_padded_sequence(y, lengths=decoder_lengths, batch_first=True, enforce_sorted=False)
    else:
        y_packed = y

    # metric
    metric = AggregationMetric(MAE())
    res = metric(y_pred, y_packed)
    if (decoder_lengths == y_pred.size(-1)).all() and y.ndim == 2:
        assert torch.isclose(res, (y.mean(0) - y_pred.mean(0)).abs().mean())


def test_none_reduction():
    pred = torch.rand(20, 10)
    target = torch.rand(20, 10)

    mae = MAE(reduction="none")(pred, target)
    assert mae.size() == pred.size(), "dimension should not change if reduction is none"


@pytest.mark.parametrize(
    ["center", "transformer"],
    itertools.product([True, False], ["log", "log1p", "softplus", "relu", "logit", None]),
)
def test_NormalDistributionLoss(center, transformer):
    mean = 1000.0
    std = 200.0
    n = 100000
    target = NormalDistributionLoss.distribution_class(loc=mean, scale=std).sample_n(n)
    if transformer in ["log", "log1p", "relu", "softplus"]:
        target = target.abs()
    normalizer = TorchNormalizer(center=center, transformer=transformer)
    normalized_target = normalizer.fit_transform(target).view(1, -1)
    target_scale = normalizer.get_parameters().unsqueeze(0)
    scale = torch.ones_like(normalized_target) * normalized_target.std()
    parameters = torch.stack(
        [normalized_target, scale],
        dim=-1,
    )
    loss = NormalDistributionLoss()
    if transformer in ["logit", "log", "log1p"]:
        with pytest.raises(AssertionError):
            rescaled_parameters = loss.rescale_parameters(parameters, target_scale=target_scale, transformer=normalizer)
    else:
        rescaled_parameters = loss.rescale_parameters(parameters, target_scale=target_scale, transformer=normalizer)
        samples = loss.sample_n(rescaled_parameters, 1)
        assert torch.isclose(torch.as_tensor(mean), samples.mean(), atol=0.1, rtol=0.2)
        if center:  # if not centered, softplus distorts std too much for testing
            assert torch.isclose(torch.as_tensor(std), samples.std(), atol=0.1, rtol=0.7)


@pytest.mark.parametrize(
    ["center", "transformer"],
    itertools.product([True, False], ["log", "log1p", "softplus", "relu", "logit", None]),
)
def test_LogNormalDistributionLoss(center, transformer):
    mean = 2.0
    std = 0.2
    n = 100000
    target = LogNormalDistributionLoss.distribution_class(loc=mean, scale=std).sample_n(n)
    normalizer = TorchNormalizer(center=center, transformer=transformer)
    normalized_target = normalizer.fit_transform(target).view(1, -1)
    target_scale = normalizer.get_parameters().unsqueeze(0)
    scale = torch.ones_like(normalized_target) * normalized_target.std()
    parameters = torch.stack(
        [normalized_target, scale],
        dim=-1,
    )
    loss = LogNormalDistributionLoss()

    if transformer not in ["log", "log1p"]:
        with pytest.raises(AssertionError):
            rescaled_parameters = loss.rescale_parameters(parameters, target_scale=target_scale, transformer=normalizer)
    else:
        rescaled_parameters = loss.rescale_parameters(parameters, target_scale=target_scale, transformer=normalizer)
        samples = loss.sample_n(rescaled_parameters, 1)
        assert torch.isclose(torch.as_tensor(mean), samples.log().mean(), atol=0.1, rtol=0.2)
        if center:  # if not centered, softplus distorts std too much for testing
            assert torch.isclose(torch.as_tensor(std), samples.log().std(), atol=0.1, rtol=0.7)


@pytest.mark.parametrize(
    ["center", "transformer"],
    itertools.product([True, False], ["log", "log1p", "softplus", "relu", "logit", None]),
)
def test_NegativeBinomialDistributionLoss(center, transformer):
    mean = 100.0
    shape = 1.0
    n = 100000
    target = NegativeBinomialDistributionLoss().map_x_to_distribution(torch.tensor([mean, shape])).sample_n(n)
    std = target.std()
    normalizer = TorchNormalizer(center=center, transformer=transformer)
    normalized_target = normalizer.fit_transform(target).view(1, -1)
    target_scale = normalizer.get_parameters().unsqueeze(0)
    parameters = torch.stack([normalized_target, 1.0 * torch.ones_like(normalized_target)], dim=-1)
    loss = NegativeBinomialDistributionLoss()

    if center or transformer in ["logit"]:
        with pytest.raises(AssertionError):
            rescaled_parameters = loss.rescale_parameters(parameters, target_scale=target_scale, transformer=normalizer)
    else:
        rescaled_parameters = loss.rescale_parameters(parameters, target_scale=target_scale, transformer=normalizer)
        samples = loss.sample_n(rescaled_parameters, 1)
        assert torch.isclose(torch.as_tensor(mean), samples.mean(), atol=0.1, rtol=0.5)
        assert torch.isclose(torch.as_tensor(std), samples.std(), atol=0.1, rtol=0.5)
