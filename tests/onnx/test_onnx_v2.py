import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest import TestCase
from unittest.mock import patch

import pytest

from parameterized import parameterized
from transformers import AutoConfig, PreTrainedTokenizerBase, is_tf_available, is_torch_available
from transformers.onnx import (
    EXTERNAL_DATA_FORMAT_SIZE_LIMIT,
    OnnxConfig,
    OnnxConfigWithPast,
    ParameterFormat,
    export,
    validate_model_outputs,
)
from transformers.onnx.utils import (
    compute_effective_axis_dimension,
    compute_serialized_parameters_size,
    get_preprocessor,
)
from transformers.testing_utils import require_onnx, require_rjieba, require_tf, require_torch, require_vision, slow


if is_torch_available() or is_tf_available():
    from transformers.onnx.features import FeaturesManager

if is_torch_available():
    import torch

    from transformers.models.deberta import modeling_deberta


@require_onnx
class OnnxUtilsTestCaseV2(TestCase):
    """
    Cover all the utilities involved to export ONNX models
    """

    @require_torch
    @patch("transformers.onnx.convert.is_torch_onnx_dict_inputs_support_available", return_value=False)
    def test_ensure_pytorch_version_ge_1_8_0(self, mock_is_torch_onnx_dict_inputs_support_available):
        """
        Ensure we raise an Exception if the pytorch version is unsupported (< 1.8.0)
        """
        self.assertRaises(AssertionError, export, None, None, None, None, None)
        mock_is_torch_onnx_dict_inputs_support_available.assert_called()

    def test_compute_effective_axis_dimension(self):
        """
        When exporting ONNX model with dynamic axis (batch or sequence) we set batch_size and/or sequence_length = -1.
        We cannot generate an effective tensor with axis dim == -1, so we trick by using some "fixed" values
        (> 1 to avoid ONNX squeezing the axis).

        This test ensure we are correctly replacing generated batch / sequence tensor with axis > 1
        """

        # Dynamic axis (batch, no token added by the tokenizer)
        self.assertEqual(compute_effective_axis_dimension(-1, fixed_dimension=2, num_token_to_add=0), 2)

        # Static axis (batch, no token added by the tokenizer)
        self.assertEqual(compute_effective_axis_dimension(0, fixed_dimension=2, num_token_to_add=0), 2)

        # Dynamic axis (sequence, token added by the tokenizer 2 (no pair))
        self.assertEqual(compute_effective_axis_dimension(0, fixed_dimension=8, num_token_to_add=2), 6)
        self.assertEqual(compute_effective_axis_dimension(0, fixed_dimension=8, num_token_to_add=2), 6)

        # Dynamic axis (sequence, token added by the tokenizer 3 (pair))
        self.assertEqual(compute_effective_axis_dimension(0, fixed_dimension=8, num_token_to_add=3), 5)
        self.assertEqual(compute_effective_axis_dimension(0, fixed_dimension=8, num_token_to_add=3), 5)

    def test_compute_parameters_serialized_size(self):
        """
        This test ensures we compute a "correct" approximation of the underlying storage requirement (size) for all the
        parameters for the specified parameter's dtype.
        """
        self.assertEqual(compute_serialized_parameters_size(2, ParameterFormat.Float), 2 * ParameterFormat.Float.size)

    def test_flatten_output_collection_property(self):
        """
        This test ensures we correctly flatten nested collection such as the one we use when returning past_keys.
        past_keys = Tuple[Tuple]

        ONNX exporter will export nested collections as ${collection_name}.${level_idx_0}.${level_idx_1}...${idx_n}
        """
        self.assertEqual(
            OnnxConfig.flatten_output_collection_property("past_key", [[0], [1], [2]]),
            {
                "past_key.0": 0,
                "past_key.1": 1,
                "past_key.2": 2,
            },
        )


class OnnxConfigTestCaseV2(TestCase):
    """
    Cover the test for models default.

    Default means no specific features is being enabled on the model.
    """

    @patch.multiple(OnnxConfig, __abstractmethods__=set())
    def test_use_external_data_format(self):
        """
        External data format is required only if the serialized size of the parameters if bigger than 2Gb
        """
        TWO_GB_LIMIT = EXTERNAL_DATA_FORMAT_SIZE_LIMIT

        # No parameters
        self.assertFalse(OnnxConfig.use_external_data_format(0))

        # Some parameters
        self.assertFalse(OnnxConfig.use_external_data_format(1))

        # Almost 2Gb parameters
        self.assertFalse(OnnxConfig.use_external_data_format((TWO_GB_LIMIT - 1) // ParameterFormat.Float.size))

        # Exactly 2Gb parameters
        self.assertTrue(OnnxConfig.use_external_data_format(TWO_GB_LIMIT))

        # More than 2Gb parameters
        self.assertTrue(OnnxConfig.use_external_data_format((TWO_GB_LIMIT + 1) // ParameterFormat.Float.size))


class OnnxConfigWithPastTestCaseV2(TestCase):
    """
    Cover the tests for model which have use_cache feature (i.e. "with_past" for ONNX)
    """

    SUPPORTED_WITH_PAST_CONFIGS = {}
    # SUPPORTED_WITH_PAST_CONFIGS = {
    #     ("BART", BartConfig),
    #     ("GPT2", GPT2Config),
    #     # ("T5", T5Config)
    # }

    @patch.multiple(OnnxConfigWithPast, __abstractmethods__=set())
    def test_use_past(self):
        """
        Ensure the use_past variable is correctly being set
        """
        for name, config in OnnxConfigWithPastTestCaseV2.SUPPORTED_WITH_PAST_CONFIGS:
            with self.subTest(name):
                self.assertFalse(
                    OnnxConfigWithPast.from_model_config(config()).use_past,
                    "OnnxConfigWithPast.from_model_config() should not use_past",
                )

                self.assertTrue(
                    OnnxConfigWithPast.with_past(config()).use_past,
                    "OnnxConfigWithPast.from_model_config() should use_past",
                )

    @patch.multiple(OnnxConfigWithPast, __abstractmethods__=set())
    def test_values_override(self):
        """
        Ensure the use_past variable correctly set the `use_cache` value in model's configuration
        """
        for name, config in OnnxConfigWithPastTestCaseV2.SUPPORTED_WITH_PAST_CONFIGS:
            with self.subTest(name):

                # without past
                onnx_config_default = OnnxConfigWithPast.from_model_config(config())
                self.assertIsNotNone(onnx_config_default.values_override, "values_override should not be None")
                self.assertIn("use_cache", onnx_config_default.values_override, "use_cache should be present")
                self.assertFalse(
                    onnx_config_default.values_override["use_cache"], "use_cache should be False if not using past"
                )

                # with past
                onnx_config_default = OnnxConfigWithPast.with_past(config())
                self.assertIsNotNone(onnx_config_default.values_override, "values_override should not be None")
                self.assertIn("use_cache", onnx_config_default.values_override, "use_cache should be present")
                self.assertTrue(
                    onnx_config_default.values_override["use_cache"], "use_cache should be False if not using past"
                )


PYTORCH_EXPORT_MODELS = {
    ("albert", "hf-internal-testing/tiny-albert"),
    ("beit", "hf-internal-testing/tiny-random-beit"),
    ("bert", "hf-internal-testing/tiny-bert"),
    ("big-bird", "hf-internal-testing/tiny-random-big_bird"),
    ("camembert", "hf-internal-testing/tiny-random-camembert"),
    ("clip", "hf-internal-testing/tiny-random-clip"),
    ("codegen", "hf-internal-testing/tiny-random-codegen"),
    ("convbert", "hf-internal-testing/tiny-random-convbert"),
    ("convnext", "hf-internal-testing/tiny-random-convnext"),
    ("data2vec-text", "hf-internal-testing/tiny-random-data2vec_text"),
    ("data2vec-vision", "hf-internal-testing/tiny-random-data2vec_vision"),
    ("deberta", "hf-internal-testing/tiny-random-deberta"),
    ("deberta-v2", "hf-internal-testing/tiny-random-deberta-v2"),
    ("deit", "hf-internal-testing/tiny-random-deit"),
    ("detr", "hf-internal-testing/tiny-random-detr"),
    ("distilbert", "hf-internal-testing/tiny-random-distilbert"),
    ("electra", "hf-internal-testing/tiny-random-electra"),
    ("groupvit", "hf-internal-testing/tiny-random-groupvit"),
    ("ibert", "hf-internal-testing/tiny-random-ibert"),
    ("layoutlm", "hf-internal-testing/tiny-random-layoutlm"),
    ("layoutlmv3", "hf-internal-testing/tiny-random-layoutlmv3"),
    ("levit", "hf-internal-testing/tiny-random-levit"),
    ("longformer", "hf-internal-testing/tiny-random-longformer"),
    ("mobilebert", "hf-internal-testing/tiny-random-mobilebert"),
    ("mobilevit", "hf-internal-testing/tiny-random-mobilevit"),
    ("owlvit", "hf-internal-testing/tiny-random-owlvit"),
    ("perceiver", "hf-internal-testing/tiny-random-language_perceiver", ("masked-lm", "sequence-classification")),
    ("perceiver", "hf-internal-testing/tiny-random-vision_perceiver_conv", ("image-classification",)),
    ("resnet", "hf-internal-testing/tiny-random-resnet"),
    ("roberta", "hf-internal-testing/tiny-random-roberta"),
    ("roformer", "hf-internal-testing/tiny-random-roformer"),
    ("segformer", "hf-internal-testing/tiny-random-segformer"),
    ("squeezebert", "hf-internal-testing/tiny-random-squeezebert"),
    ("vit", "hf-internal-testing/tiny-random-vit"),
    ("xlm", "hf-internal-testing/tiny-random-xlm"),
    ("xlm-roberta", "hf-internal-testing/tiny-random-xlm-roberta"),
    ("yolos", "hf-internal-testing/tiny-random-yolos"),
}

PYTORCH_EXPORT_WITH_PAST_MODELS = {
    ("bloom", "hf-internal-testing/tiny-random-bloom"),
    ("gpt-neo", "hf-internal-testing/tiny-random-gpt2"),
    ("gpt2", "hf-internal-testing/tiny-random-gpt2"),
}

PYTORCH_EXPORT_SEQ2SEQ_WITH_PAST_MODELS = {
    ("bart", "hf-internal-testing/tiny-random-bart"),
    ("bigbird-pegasus", "hf-internal-testing/tiny-random-bigbird_pegasus"),
    ("blenderbot", "hf-internal-testing/tiny-random-blenderbot"),
    ("blenderbot-small", "hf-internal-testing/tiny-random-blenderbot-small"),
    ("longt5", "hf-internal-testing/tiny-random-longt5"),
    ("m2m-100", "hf-internal-testing/tiny-random-m2m_100"),
    ("marian", "hf-internal-testing/tiny-random-marian"),
    ("mbart", "hf-internal-testing/tiny-random-mbart"),
    ("mt5", "hf-internal-testing/tiny-random-mt5"),
    ("t5", "hf-internal-testing/tiny-random-t5"),
}

# TODO(lewtun): Include the same model types in `PYTORCH_EXPORT_MODELS` once TensorFlow has parity with the PyTorch model implementations.
TENSORFLOW_EXPORT_DEFAULT_MODELS = {
    ("albert", "hf-internal-testing/tiny-albert"),
    ("bert", "hf-internal-testing/tiny-bert"),
    ("camembert", "hf-internal-testing/tiny-camembert"),
    ("distilbert", "hf-internal-testing/tiny-distilbert"),
    ("roberta", "hf-internal-testing/tiny-roberta"),
}

# TODO(lewtun): Include the same model types in `PYTORCH_EXPORT_WITH_PAST_MODELS` once TensorFlow has parity with the PyTorch model implementations.
TENSORFLOW_EXPORT_WITH_PAST_MODELS = {}

# TODO(lewtun): Include the same model types in `PYTORCH_EXPORT_SEQ2SEQ_WITH_PAST_MODELS` once TensorFlow has parity with the PyTorch model implementations.
TENSORFLOW_EXPORT_SEQ2SEQ_WITH_PAST_MODELS = {}


def _get_models_to_test(export_models_list):
    models_to_test = []
    if is_torch_available() or is_tf_available():
        for name, model, *features in export_models_list:
            if features:
                feature_config_mapping = {
                    feature: FeaturesManager.get_config(name, feature) for _ in features for feature in _
                }
            else:
                feature_config_mapping = FeaturesManager.get_supported_features_for_model_type(name)

            for feature, onnx_config_class_constructor in feature_config_mapping.items():
                models_to_test.append((f"{name}_{feature}", name, model, feature, onnx_config_class_constructor))
        return sorted(models_to_test)
    else:
        # Returning some dummy test that should not be ever called because of the @require_torch / @require_tf
        # decorators.
        # The reason for not returning an empty list is because parameterized.expand complains when it's empty.
        return [("dummy", "dummy", "dummy", "dummy", OnnxConfig.from_model_config)]


class OnnxExportTestCaseV2(TestCase):
    """
    Integration tests ensuring supported models are correctly exported
    """

    def _onnx_export(self, test_name, name, model_name, feature, onnx_config_class_constructor, device="cpu"):
        from transformers.onnx import export

        model_class = FeaturesManager.get_model_class_for_feature(feature)
        config = AutoConfig.from_pretrained(model_name)
        model = model_class.from_config(config)

        # Dynamic axes aren't supported for YOLO-like models. This means they cannot be exported to ONNX on CUDA devices.
        # See: https://github.com/ultralytics/yolov5/pull/8378
        if model.__class__.__name__.startswith("Yolos") and device != "cpu":
            return

        onnx_config = onnx_config_class_constructor(model.config)

        if is_torch_available():
            from transformers.utils import torch_version

            if torch_version < onnx_config.torch_onnx_minimum_version:
                pytest.skip(
                    "Skipping due to incompatible PyTorch version. Minimum required is"
                    f" {onnx_config.torch_onnx_minimum_version}, got: {torch_version}"
                )

        preprocessor = get_preprocessor(model_name)

        # Useful for causal lm models that do not use pad tokens.
        if isinstance(preprocessor, PreTrainedTokenizerBase) and not getattr(config, "pad_token_id", None):
            config.pad_token_id = preprocessor.eos_token_id

        with NamedTemporaryFile("w") as output:
            try:
                onnx_inputs, onnx_outputs = export(
                    preprocessor, model, onnx_config, onnx_config.default_onnx_opset, Path(output.name), device=device
                )
                validate_model_outputs(
                    onnx_config,
                    preprocessor,
                    model,
                    Path(output.name),
                    onnx_outputs,
                    onnx_config.atol_for_validation,
                )
            except (RuntimeError, ValueError) as e:
                self.fail(f"{name}, {feature} -> {e}")

    @parameterized.expand(_get_models_to_test(PYTORCH_EXPORT_MODELS))
    @slow
    @require_torch
    @require_vision
    @require_rjieba
    def test_pytorch_export(self, test_name, name, model_name, feature, onnx_config_class_constructor):
        self._onnx_export(test_name, name, model_name, feature, onnx_config_class_constructor)

    @parameterized.expand(_get_models_to_test(PYTORCH_EXPORT_MODELS))
    @slow
    @require_torch
    @require_vision
    @require_rjieba
    def test_pytorch_export_on_cuda(self, test_name, name, model_name, feature, onnx_config_class_constructor):
        self._onnx_export(test_name, name, model_name, feature, onnx_config_class_constructor, device="cuda")

    @parameterized.expand(_get_models_to_test(PYTORCH_EXPORT_WITH_PAST_MODELS))
    @slow
    @require_torch
    def test_pytorch_export_with_past(self, test_name, name, model_name, feature, onnx_config_class_constructor):
        self._onnx_export(test_name, name, model_name, feature, onnx_config_class_constructor)

    @parameterized.expand(_get_models_to_test(PYTORCH_EXPORT_SEQ2SEQ_WITH_PAST_MODELS))
    @slow
    @require_torch
    def test_pytorch_export_seq2seq_with_past(
        self, test_name, name, model_name, feature, onnx_config_class_constructor
    ):
        self._onnx_export(test_name, name, model_name, feature, onnx_config_class_constructor)

    @parameterized.expand(_get_models_to_test(TENSORFLOW_EXPORT_DEFAULT_MODELS))
    @slow
    @require_tf
    @require_vision
    def test_tensorflow_export(self, test_name, name, model_name, feature, onnx_config_class_constructor):
        self._onnx_export(test_name, name, model_name, feature, onnx_config_class_constructor)

    @parameterized.expand(_get_models_to_test(TENSORFLOW_EXPORT_WITH_PAST_MODELS), skip_on_empty=True)
    @slow
    @require_tf
    def test_tensorflow_export_with_past(self, test_name, name, model_name, feature, onnx_config_class_constructor):
        self._onnx_export(test_name, name, model_name, feature, onnx_config_class_constructor)

    @parameterized.expand(_get_models_to_test(TENSORFLOW_EXPORT_SEQ2SEQ_WITH_PAST_MODELS), skip_on_empty=True)
    @slow
    @require_tf
    def test_tensorflow_export_seq2seq_with_past(
        self, test_name, name, model_name, feature, onnx_config_class_constructor
    ):
        self._onnx_export(test_name, name, model_name, feature, onnx_config_class_constructor)


class StableDropoutTestCase(TestCase):
    """Tests export of StableDropout module."""

    @require_torch
    @pytest.mark.filterwarnings("ignore:.*Dropout.*:UserWarning:torch.onnx.*")  # torch.onnx is spammy.
    def test_training(self):
        """Tests export of StableDropout in training mode."""
        devnull = open(os.devnull, "wb")
        # drop_prob must be > 0 for the test to be meaningful
        sd = modeling_deberta.StableDropout(0.1)
        # Avoid warnings in training mode
        do_constant_folding = False
        # Dropout is a no-op in inference mode
        training = torch.onnx.TrainingMode.PRESERVE
        input = (torch.randn(2, 2),)

        torch.onnx.export(
            sd,
            input,
            devnull,
            opset_version=12,  # Minimum supported
            do_constant_folding=do_constant_folding,
            training=training,
        )

        # Expected to fail with opset_version < 12
        with self.assertRaises(Exception):
            torch.onnx.export(
                sd,
                input,
                devnull,
                opset_version=11,
                do_constant_folding=do_constant_folding,
                training=training,
            )
