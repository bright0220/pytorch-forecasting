from copy import deepcopy
import itertools
import pickle
from typing import Dict

import numpy as np
import pandas as pd
import pytest
from sklearn.preprocessing import StandardScaler
import torch

from pytorch_forecasting.data import (
    EncoderNormalizer,
    GroupNormalizer,
    NaNLabelEncoder,
    TimeSeriesDataSet,
    TimeSynchronizedBatchSampler,
)
from pytorch_forecasting.data.examples import get_stallion_data
from pytorch_forecasting.data.timeseries import _find_end_indices

torch.manual_seed(23)


@pytest.mark.parametrize(
    "data,allow_nan",
    itertools.product(
        [
            (np.array([2, 3, 4]), np.array([1, 2, 3, 5, np.nan])),
            (np.array(["a", "b", "c"]), np.array(["q", "a", "nan"])),
        ],
        [True, False],
    ),
)
def test_NaNLabelEncoder(data, allow_nan):
    fit_data, transform_data = data
    encoder = NaNLabelEncoder(warn=False, add_nan=allow_nan)
    encoder.fit(fit_data)
    assert np.array_equal(
        encoder.inverse_transform(encoder.transform(fit_data)), fit_data
    ), "Inverse transform should reverse transform"
    if not allow_nan:
        with pytest.raises(KeyError):
            encoder.transform(transform_data)
    else:
        assert encoder.transform(transform_data)[0] == 0, "First value should be translated to 0 if nan"
        assert encoder.transform(transform_data)[-1] == 0, "Last value should be translated to 0 if nan"
        assert encoder.transform(fit_data)[0] > 0, "First value should not be 0 if not nan"


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(method="robust"),
        dict(transformer="log"),
        dict(center=False),
    ],
)
def test_EncoderNormalizer(kwargs):
    data = torch.rand(100)
    defaults = dict(method="standard", center=True)
    defaults.update(kwargs)
    kwargs = defaults
    if kwargs["coerce_positive"] and kwargs["log_scale"]:
        with pytest.raises(AssertionError):
            normalizer = EncoderNormalizer(**kwargs)
    else:
        normalizer = EncoderNormalizer(**kwargs)
        if kwargs["coerce_positive"]:
            data = data - 0.5

        if kwargs["coerce_positive"]:
            assert (
                normalizer.inverse_transform(normalizer.fit_transform(data)) >= 0
            ).all(), "Inverse transform should yield only positive values"
        else:
            assert torch.isclose(
                normalizer.inverse_transform(normalizer.fit_transform(data)), data, atol=1e-5
            ).all(), "Inverse transform should reverse transform"


@pytest.mark.parametrize(
    "kwargs,groups",
    itertools.product(
        [
            dict(method="robust"),
            dict(log_scale=True),
            dict(coerce_positive=True),
            dict(center=False),
            dict(log_zero_value=0.0),
            dict(scale_by_group=True),
        ],
        [[], ["a"]],
    ),
)
def test_GroupNormalizer(kwargs, groups):
    data = pd.DataFrame(dict(a=[1, 1, 2, 2, 3], b=[1.1, 1.1, 1.0, 5.0, 1.1]))
    defaults = dict(
        method="standard", log_scale=False, coerce_positive=False, center=True, log_zero_value=0.0, scale_by_group=False
    )
    defaults.update(kwargs)
    kwargs = defaults
    kwargs["groups"] = groups
    kwargs["scale_by_group"] = kwargs["scale_by_group"] and len(kwargs["groups"]) > 0

    if kwargs["coerce_positive"] and kwargs["log_scale"]:
        with pytest.raises(AssertionError):
            normalizer = GroupNormalizer(**kwargs)
    else:
        if kwargs["coerce_positive"]:
            data.b = data.b - 2.0
        normalizer = GroupNormalizer(**kwargs)
        encoded = normalizer.fit_transform(data["b"], data)

        test_data = dict(
            prediction=torch.tensor([encoded.iloc[0]]),
            target_scale=torch.tensor(normalizer.get_parameters([1])).unsqueeze(0),
        )

        if kwargs["coerce_positive"]:
            assert (normalizer(test_data) >= 0).all(), "Inverse transform should yield only positive values"
        else:
            assert torch.isclose(
                normalizer(test_data), torch.tensor(data.b.iloc[0]), atol=1e-5
            ).all(), "Inverse transform should reverse transform"


def check_dataloader_output(dataset: TimeSeriesDataSet, out: Dict[str, torch.Tensor]):
    x, y = out

    # check for nans and finite
    for k, v in x.items():
        assert torch.isfinite(v).all(), f"Values for {k} should be finite"
        assert not torch.isnan(v).any(), f"Values for {k} should not be nan"
    assert torch.isfinite(y).all(), "Values for target should be finite"
    assert not torch.isnan(y).any(), "Values for target should not be nan"

    # check shape
    assert x["encoder_cont"].size(2) == len(dataset.reals)
    assert x["encoder_cat"].size(2) == len(dataset.flat_categoricals)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(min_encoder_length=0, max_prediction_length=2),
        dict(static_categoricals=["agency", "sku"]),
        dict(static_reals=["avg_population_2017", "avg_yearly_household_income_2017"]),
        dict(time_varying_known_categoricals=["month"]),
        dict(
            time_varying_known_categoricals=["special_days", "month"],
            variable_groups=dict(
                special_days=[
                    "easter_day",
                    "good_friday",
                    "new_year",
                    "christmas",
                    "labor_day",
                    "independence_day",
                    "revolution_day_memorial",
                    "regional_games",
                    "fifa_u_17_world_cup",
                    "football_gold_cup",
                    "beer_capital",
                    "music_fest",
                ]
            ),
        ),
        dict(time_varying_known_reals=["time_idx", "price_regular", "discount_in_percent"]),
        dict(time_varying_unknown_reals=["volume", "log_volume", "industry_volume", "soda_volume", "avg_max_temp"]),
        dict(
            target_normalizer=GroupNormalizer(
                groups=["agency", "sku"], log_scale=True, scale_by_group=True, log_zero_value=1.0
            )
        ),
        dict(target_normalizer=EncoderNormalizer(), min_encoder_length=2),
        dict(randomize_length=True, min_encoder_length=2, min_prediction_length=1),
        dict(predict_mode=True),
        dict(add_target_scales=True),
        dict(add_encoder_length=True),
        dict(add_encoder_length=True),
        dict(add_relative_time_idx=True),
        dict(weight="volume"),
        dict(
            scalers=dict(time_idx=GroupNormalizer(), price_regular=StandardScaler()),
            categorical_encoders=dict(month=NaNLabelEncoder()),
            time_varying_known_categoricals=["month"],
            time_varying_known_reals=["time_idx", "price_regular"],
        ),
        dict(dropout_categoricals=["month"], time_varying_known_categoricals=["month"]),
        dict(constant_fill_strategy=dict(volume=0.0), allow_missings=True),
        dict(target_normalizer=None),
    ],
)
def test_TimeSeriesDataSet(test_data, kwargs):

    defaults = dict(
        time_idx="time_idx",
        target="volume",
        group_ids=["agency", "sku"],
        max_encoder_length=5,
        max_prediction_length=2,
    )
    defaults.update(kwargs)
    kwargs = defaults

    if kwargs.get("allow_missings", False):
        np.random.seed(2)
        test_data = test_data.sample(frac=0.5)
        defaults["min_encoder_length"] = 0
        defaults["min_prediction_length"] = 1

    # create dataset and sample from it
    dataset = TimeSeriesDataSet(test_data, **kwargs)
    check_dataloader_output(dataset, next(iter(dataset.to_dataloader(num_workers=0))))


def test_from_dataset(test_dataset, test_data):
    dataset = TimeSeriesDataSet.from_dataset(test_dataset, test_data)
    check_dataloader_output(dataset, next(iter(dataset.to_dataloader(num_workers=0))))


def test_from_dataset_equivalence(test_data):
    training = TimeSeriesDataSet(
        test_data[lambda x: x.time_idx < x.time_idx.max() - 1],
        time_idx="time_idx",
        target="volume",
        time_varying_known_reals=["price_regular", "time_idx"],
        group_ids=["agency", "sku"],
        static_categoricals=["agency"],
        max_encoder_length=3,
        max_prediction_length=2,
        min_prediction_length=1,
        min_encoder_length=0,
        randomize_length=None,
        add_encoder_length=True,
        add_relative_time_idx=True,
        add_target_scales=True,
    )
    validation1 = TimeSeriesDataSet.from_dataset(training, test_data, predict=True)
    validation2 = TimeSeriesDataSet.from_dataset(
        training,
        test_data[lambda x: x.time_idx > x.time_idx.min() + 2],
        predict=True,
    )
    # ensure validation1 and validation2 datasets are exactly the same despite different data inputs
    for v1, v2 in zip(iter(validation1.to_dataloader(train=False)), iter(validation2.to_dataloader(train=False))):
        for k in v1[0].keys():
            assert torch.isclose(v1[0][k], v2[0][k]).all()
        assert torch.isclose(v1[1], v2[1]).all()


def test_dataset_index(test_dataset):
    index = []
    for x, _ in iter(test_dataset.to_dataloader()):
        index.append(test_dataset.x_to_index(x))
    index = pd.concat(index, axis=0, ignore_index=True)
    assert len(index) <= len(test_dataset), "Index can only be subset of dataset"


@pytest.mark.parametrize(
    "value,variable,target",
    [
        (1.0, "price_regular", "encoder"),
        (1.0, "price_regular", "all"),
        (1.0, "price_regular", "decoder"),
        ("Agency_01", "agency", "all"),
        ("Agency_01", "agency", "decoder"),
    ],
)
def test_overwrite_values(test_dataset, value, variable, target):
    dataset = deepcopy(test_dataset)

    # create variables to check against
    control_outputs = next(iter(dataset.to_dataloader(num_workers=0, train=False)))
    dataset.set_overwrite_values(value, variable=variable, target=target)

    # test change
    outputs = next(iter(dataset.to_dataloader(num_workers=0, train=False)))
    check_dataloader_output(dataset, outputs)

    if variable in dataset.reals:
        output_name_suffix = "cont"
    else:
        output_name_suffix = "cat"

    if target == "all":
        output_names = [f"encoder_{output_name_suffix}", f"decoder_{output_name_suffix}"]
    else:
        output_names = [f"{target}_{output_name_suffix}"]

    for name in outputs[0].keys():
        changed = torch.isclose(outputs[0][name], control_outputs[0][name]).all()
        if name in output_names or (
            "cat" in name and variable == "agency"
        ):  # exception for static categorical which should always change
            assert not changed, f"Output {name} should change"
        else:
            assert changed, f"Output {name} should not change"

    # test resetting
    dataset.reset_overwrite_values()
    outputs = next(iter(dataset.to_dataloader(num_workers=0, train=False)))
    for name in outputs[0].keys():
        changed = torch.isclose(outputs[0][name], control_outputs[0][name]).all()
        assert changed, f"Output {name} should be reset"
    assert torch.isclose(outputs[1], control_outputs[1]).all(), "Target should be reset"


@pytest.mark.parametrize(
    "drop_last,shuffle,as_string,batch_size",
    [
        (True, True, True, 64),
        (False, False, False, 64),
        (True, False, False, 1000),
    ],
)
def test_TimeSynchronizedBatchSampler(test_dataset, shuffle, drop_last, as_string, batch_size):
    if as_string:
        dataloader = test_dataset.to_dataloader(
            batch_sampler="synchronized", shuffle=shuffle, drop_last=drop_last, batch_size=batch_size
        )
    else:
        sampler = TimeSynchronizedBatchSampler(
            data_source=test_dataset, shuffle=shuffle, drop_last=drop_last, batch_size=batch_size
        )
        dataloader = test_dataset.to_dataloader(batch_sampler=sampler)

    time_idx_pos = test_dataset.reals.index("time_idx")
    for x, _ in iter(dataloader):  # check all samples
        time_idx_of_first_prediction = x["decoder_cont"][:, 0, time_idx_pos]
        assert torch.isclose(
            time_idx_of_first_prediction, time_idx_of_first_prediction[0]
        ).all(), "Time index should be the same for the first prediction"


def test_find_end_indices():
    diffs = np.array([1, 1, 1, 1, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 1, 1, 1, 1, 1, 1])
    max_lengths = np.array([4, 4, 4, 4, 4, 4, 4, 4, 3, 2, 1, 4, 4, 4, 4, 4, 4, 4, 4, 3, 2, 1])

    ends, missings = _find_end_indices(diffs, max_lengths, min_length=3)
    ends_test = np.array([3, 4, 4, 5, 6, 8, 9, 10, 10, 10, 10, 14, 15, 15, 16, 17, 19, 20, 21, 21, 21, 21])
    missings_test = np.array([[0, 2], [5, 7], [11, 13], [16, 18]])
    np.testing.assert_array_equal(ends, ends_test)
    np.testing.assert_array_equal(missings, missings_test)


def test_raise_short_encoder_length(test_data):
    with pytest.warns(UserWarning):
        test_data = test_data[lambda x: ~((x.agency == "Agency_22") & (x.sku == "SKU_01") & (x.time_idx > 3))]
        TimeSeriesDataSet(
            test_data,
            time_idx="time_idx",
            target="volume",
            group_ids=["agency", "sku"],
            max_encoder_length=5,
            max_prediction_length=2,
            min_prediction_length=1,
            min_encoder_length=5,
        )


def test_categorical_target(test_data):
    dataset = TimeSeriesDataSet(
        test_data,
        time_idx="time_idx",
        target="agency",
        group_ids=["agency", "sku"],
        max_encoder_length=5,
        max_prediction_length=2,
        min_prediction_length=1,
        min_encoder_length=1,
    )

    _, y = next(iter(dataset.to_dataloader()))
    assert y.dtype is torch.long, "target must be of type long"


def test_pickle(test_dataset):
    pickle.dumps(test_dataset)
    pickle.dumps(test_dataset.to_dataloader())


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        dict(
            target_normalizer=GroupNormalizer(
                groups=["agency", "sku"], log_scale=True, scale_by_group=True, log_zero_value=1.0
            ),
        ),
    ],
)
def test_new_group_ids(test_data, kwargs):
    """Test for new group ids in dataset"""
    train_agency = test_data["agency"].iloc[0]
    train_dataset = TimeSeriesDataSet(
        test_data[lambda x: x.agency == train_agency],
        time_idx="time_idx",
        target="volume",
        group_ids=["agency", "sku"],
        max_encoder_length=5,
        max_prediction_length=2,
        min_prediction_length=1,
        min_encoder_length=1,
        categorical_encoders=dict(agency=NaNLabelEncoder(add_nan=True), sku=NaNLabelEncoder(add_nan=True)),
        **kwargs,
    )

    # test sampling from training dataset
    next(iter(train_dataset.to_dataloader()))

    # create test dataset with group ids that have not been observed before
    test_dataset = TimeSeriesDataSet.from_dataset(train_dataset, test_data)

    # check that we can iterate through dataset without error
    for _ in iter(test_dataset.to_dataloader()):
        pass


def test_timeseries_columns_naming(test_data):
    with pytest.raises(ValueError):
        TimeSeriesDataSet(
            test_data.rename(columns=dict(agency="agency.2")),
            time_idx="time_idx",
            target="volume",
            group_ids=["agency.2", "sku"],
            max_encoder_length=5,
            max_prediction_length=2,
            min_prediction_length=1,
            min_encoder_length=1,
        )


def test_encoder_normalizer_for_covariates(test_data):
    dataset = TimeSeriesDataSet(
        test_data,
        time_idx="time_idx",
        target="volume",
        group_ids=["agency", "sku"],
        max_encoder_length=5,
        max_prediction_length=2,
        min_prediction_length=1,
        min_encoder_length=1,
        time_varying_known_reals=["price_regular"],
        scalers={"price_regular": EncoderNormalizer()},
    )
    next(iter(dataset.to_dataloader()))
