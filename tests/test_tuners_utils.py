#!/usr/bin/env python3

# coding=utf-8
# Copyright 2023-present the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import unittest

from parameterized import parameterized
from transformers import AutoModel

from peft import IA3Config, LoraConfig, get_peft_model
from peft.tuners.tuners_utils import (
    _maybe_include_all_linear_layers,
    check_target_module_exists,
    inspect_matched_modules,
)

from .testing_utils import require_bitsandbytes, require_torch_gpu

# Implements tests for regex matching logic common for all BaseTuner subclasses, and
# tests for correct behaviour with different config kwargs for BaseTuners (Ex: feedforward for IA3, etc) and
# tests for utlity function to include all linear layers

REGEX_TEST_CASES = [
    # tuple of
    # 1. key
    # 2. target_modules
    # 3. layers_to_transform
    # 4. layers_pattern
    # 5. expected result
    # some basic examples
    ("", [], None, None, False),
    ("", ["foo"], None, None, False),
    ("foo", [], None, None, False),
    ("foo", ["foo"], None, None, True),
    ("foo", ["bar"], None, None, False),
    ("foo", ["foo", "bar"], None, None, True),
    # with regex
    ("foo", "foo", None, None, True),
    ("foo", ".*oo", None, None, True),
    ("foo", "fo.*", None, None, True),
    ("foo", ".*bar.*", None, None, False),
    ("foobar", ".*oba.*", None, None, True),
    # with layers_to_transform
    ("foo.bar.1.baz", ["baz"], [1], ["bar"], True),
    ("foo.bar.1.baz", ["baz"], [0], ["bar"], False),
    ("foo.bar.1.baz", ["baz"], [2], ["bar"], False),
    ("foo.bar.10.baz", ["baz"], [0], ["bar"], False),
    ("foo.bar.10.baz", ["baz"], [1], ["bar"], False),
    ("foo.bar.1.baz", ["baz"], [0, 1, 2], ["bar"], True),
    ("foo.bar.1.baz", ["baz", "spam"], [1], ["bar"], True),
    ("foo.bar.1.baz", ["baz", "spam"], [0, 1, 2], ["bar"], True),
    # TODO: Unclear what expected behaviour is when layers_pattern is an empty list.
    # Currently, an empty layers_pattern leads to all layer indexes being matched,
    # which means layers_to_transform is ignored.
    ("foo.bar.1.baz", ["baz"], [1], [], True),
    # TODO: Below test currently fails, again because of empty layers_pattern
    # layers_to_transform is 0, but layers_pattern is empty, so all layer indexes are matched
    # ("foo.bar.1.baz", ["baz"], [0], [], False),
    ("foo.bar.1.baz", ["baz"], [1], ["ar"], True),
    # some realistic examples: transformers model
    ("transformer.h.1.attn.attention.q_proj.foo", ["q_proj"], None, [], False),
    ("transformer.h.1.attn.attention.q_proj", [], None, [], False),
    ("transformer.h.1.attn.attention.q_proj", ["q_proj"], None, [], True),
    ("transformer.h.1.attn.attention.q_proj", ["q_proj", "v_proj"], None, [], True),
    ("transformer.h.1.attn.attention.resid_dropout", ["q_proj", "v_proj"], None, [], False),
    ("transformer.h.1.attn.attention.q_proj", ["q_proj"], [1], ["h"], True),
    ("transformer.h.1.attn.attention.q_proj", ["q_proj"], [0], ["h"], False),
    ("transformer.h.1.attn.attention.q_proj", ["q_proj"], [2], ["h"], False),
    ("transformer.h.1.attn.attention.q_proj", ["q_proj"], [0, 1, 2], ["h"], True),
    ("transformer.h.1.attn.attention.q_proj", ["q_proj", "v_proj"], [0, 1, 2], ["h"], True),
    ("foo.bar.q_proj", ["q_proj"], None, [], True),
    ("foo.bar.1.baz", ["baz"], [1], ["foo"], False),
    # other corner cases. For ex, below is a case where layers_pattern
    # is one of the target nn.modules
    ("foo.bar.1.baz", ["baz"], [1], ["baz"], False),
    # here, layers_pattern is 'bar', but only keys that contain '.bar' are valid.
    ("bar.1.baz", ["baz"], [1], ["bar"], False),
    ("foo.bar.001.baz", ["baz"], [1], ["bar"], True),
    ("foo.bar.1.spam.2.baz", ["baz"], [1], ["bar"], True),
    ("foo.bar.2.spam.1.baz", ["baz"], [1], ["bar"], False),
    # some realistic examples: module using nn.Sequential
    # for the below test case, key should contain '.blocks' to be valid, because of how layers_pattern is matched
    ("blocks.1.weight", ["weight"], [1], ["blocks"], False),
    ("blocks.1.bias", ["weight"], [1], ["blocks"], False),
    ("mlp.blocks.1.weight", ["weight"], [1], ["blocks"], True),
    ("mlp.blocks.1.bias", ["weight"], [1], ["blocks"], False),
]

MAYBE_INCLUDE_ALL_LINEAR_LAYERS_TEST_CASES = [
    # model_name, initial_target_modules, final_expected_target_modules
    ("HuggingFaceH4/tiny-random-LlamaForCausalLM", ["k_proj"], ["k_proj"]),
    # test with target_modules as None
    ("HuggingFaceH4/tiny-random-LlamaForCausalLM", None, None),
    # test with target_modules as a regex expression
    ("HuggingFaceH4/tiny-random-LlamaForCausalLM", ".*(q|v)$", ".*(q|v)$"),
    # test for a causal Llama model
    (
        "HuggingFaceH4/tiny-random-LlamaForCausalLM",
        "ALL",
        ["k_proj", "v_proj", "q_proj", "o_proj", "down_proj", "up_proj", "gate_proj"],
    ),
    # test for gpt2 with Conv1D layers
    ("hf-internal-testing/tiny-random-gpt2", "ALL", ["c_attn", "c_proj", "c_fc"]),
    # test for T5 model
    ("hf-internal-testing/tiny-random-t5", "ALL", ["k", "q", "v", "o", "wi", "wo"]),
]

bnb_quantizations = [("4bit",), ("8bit",)]
bnb_test_cases = [(x + y) for x in MAYBE_INCLUDE_ALL_LINEAR_LAYERS_TEST_CASES for y in bnb_quantizations]


class PeftCustomKwargsTester(unittest.TestCase):
    r"""
    Test if the PeftModel is instantiated with correct behaviour for custom kwargs. This includes:
    - test if regex matching works correctly
    - test if adapters handle custom kwargs the right way e.g. IA3 for `feedforward_modules`

    """

    transformers_class = AutoModel

    @parameterized.expand(REGEX_TEST_CASES)
    def test_regex_matching_valid(self, key, target_modules, layers_to_transform, layers_pattern, expected_result):
        # We use a LoRA Config for testing, but the regex matching function is common for all BaseTuner subclasses.
        # example model_id for config initialization. key is matched only against the target_modules given, so this can be any model
        model_id = "peft-internal-testing/tiny-OPTForCausalLM-lora"
        config = LoraConfig(
            base_model_name_or_path=model_id,
            target_modules=target_modules,
            layers_pattern=layers_pattern,
            layers_to_transform=layers_to_transform,
        )
        actual_result = bool(check_target_module_exists(config, key))
        self.assertEqual(actual_result, expected_result)

    def test_module_matching_lora(self):
        # peft models that have a module matching method to inspect the matching modules to allow
        # users to easily debug their configuration. Here we only test a single case, not all possible combinations of
        # configs that could exist. This is okay as the method calls `check_target_module_exists` internally, which
        # has been extensively tested above.
        model_id = "hf-internal-testing/tiny-random-BloomForCausalLM"
        model = self.transformers_class.from_pretrained(model_id)
        # by default, this model matches query_key_value
        config = LoraConfig()
        peft_model = get_peft_model(model, config)

        output = inspect_matched_modules(peft_model)  # inspects default adapter for peft_model
        matched = output["matched"]
        expected = [
            "h.0.self_attention.query_key_value",
            "h.1.self_attention.query_key_value",
            "h.2.self_attention.query_key_value",
            "h.3.self_attention.query_key_value",
            "h.4.self_attention.query_key_value",
        ]
        self.assertEqual(matched, expected)  # module lists should match exactly

        # no overlap with matched modules
        unmatched = output["unmatched"]
        for key in expected:
            self.assertFalse(key in unmatched)

    def test_feedforward_matching_ia3(self):
        model_id = "hf-internal-testing/tiny-random-T5ForConditionalGeneration"
        model = self.transformers_class.from_pretrained(model_id)
        # simple example for just one t5 block for testing
        config_kwargs = {
            "target_modules": ".*encoder.*block.0.*(SelfAttention|EncDecAttention|DenseReluDense).(k|q|v|wo|wi)$",
            "feedforward_modules": ["wo", "wi"],
        }
        config = IA3Config(base_model_name_or_path=model_id, **config_kwargs)
        peft_model = get_peft_model(model, config)
        output = inspect_matched_modules(peft_model)  # inspects default adapter for peft_model
        matched = output["matched"]
        expected = [
            "encoder.block.0.layer.0.SelfAttention.q",
            "encoder.block.0.layer.0.SelfAttention.k",
            "encoder.block.0.layer.0.SelfAttention.v",
            "encoder.block.0.layer.1.DenseReluDense.wi",
            "encoder.block.0.layer.1.DenseReluDense.wo",
        ]
        expected_feedforward = [
            "encoder.block.0.layer.1.DenseReluDense.wi",
            "encoder.block.0.layer.1.DenseReluDense.wo",
        ]
        self.assertEqual(matched, expected)  # not required since we do similar checks above, but just to be sure
        module_dict = dict(model.named_modules())
        for key in matched:
            module = module_dict[key]
            if key in expected_feedforward:
                self.assertTrue(module.is_feedforward)
            else:  # other IA3 modules should not be marked as feedforward
                self.assertFalse(module.is_feedforward)

    @parameterized.expand(MAYBE_INCLUDE_ALL_LINEAR_LAYERS_TEST_CASES)
    def test_maybe_include_all_linear_layers(self, model_id, initial_target_modules, expected_target_modules):
        model = self.transformers_class.from_pretrained(model_id)
        self._check_match_with_expected_target_modules(model_id, model, initial_target_modules, expected_target_modules)

    @parameterized.expand(bnb_test_cases)
    @require_torch_gpu
    @require_bitsandbytes
    def test_maybe_include_all_linear_layers_bnb(self, model_id, initial_target_modules, expected_target_modules, quantization):
        if quantization == "4bit":
            config_kwargs = {"load_in_4bit" :  True}
        elif quantization == "8bit":
            config_kwargs = {"load_in_8bit" : True}
        model = self.transformers_class.from_pretrained(model_id, device_map="auto", **config_kwargs)
        self._check_match_with_expected_target_modules(model_id, model, initial_target_modules, expected_target_modules, **config_kwargs)
    
    def _check_match_with_expected_target_modules(self, model_id, model, initial_target_modules, expected_target_modules, **config_kwargs):
        """
        Helper function for the test for `_maybe_include_all_linear_layers`
        """
        lora_config = LoraConfig(base_model_name_or_path=model_id, target_modules=initial_target_modules, **config_kwargs)
        new_lora_config = _maybe_include_all_linear_layers(lora_config, model)
        if isinstance(expected_target_modules, list):
            # assert that expected and actual target_modules have the same items
            self.assertCountEqual(new_lora_config.target_modules, expected_target_modules)
        else:
            self.assertEqual(new_lora_config.target_modules, expected_target_modules)
 