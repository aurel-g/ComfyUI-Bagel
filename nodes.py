import os
from copy import deepcopy
from typing import (
    Any,
    AsyncIterable,
    Callable,
    Dict,
    Generator,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)
import requests
from io import BytesIO
import random
import numpy as np

from PIL import Image
import torch
from accelerate import infer_auto_device_map, load_checkpoint_and_dispatch, init_empty_weights
from safetensors.torch import load_file

from .data.transforms import ImageTransform
from .data.data_utils import pil_img2rgb, add_special_tokens
from .modeling.bagel import (
    BagelConfig, Bagel, Qwen2Config, Qwen2ForCausalLM, SiglipVisionConfig, SiglipVisionModel
)
from .modeling.qwen2 import Qwen2Tokenizer
from .modeling.bagel.qwen2_navit import NaiveCache
from .modeling.autoencoder import load_ae
from .inferencer import InterleaveInferencer


class LoadBAGELModel:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model_path": ("STRING", {"default": "./BAGEL-7B-MoT"}),
            }
        }

    RETURN_TYPES = ("MODEL", "VAEMODEL", "TOKENIZER", "VAETRANSFORM", "VITTRANSFORM", "TOKENIDS",)
    RETURN_NAMES = ("model", "vae_model", "tokenizer", "vae_transform", "vit_transform", "new_token_ids",)
    FUNCTION = "load_model"
    CATEGORY = "BAGEL"

    def load_model(self, model_path):

        # LLM config preparing
        llm_config = Qwen2Config.from_json_file(os.path.join(model_path, "llm_config.json"))
        llm_config.qk_norm = True
        llm_config.tie_word_embeddings = False
        llm_config.layer_module = "Qwen2MoTDecoderLayer"
        
        # ViT config preparing
        vit_config = SiglipVisionConfig.from_json_file(os.path.join(model_path, "vit_config.json"))
        vit_config.rope = False
        vit_config.num_hidden_layers = vit_config.num_hidden_layers - 1
        
        # VAE loading
        vae_model, vae_config = load_ae(local_path=os.path.join(model_path, "ae.safetensors"))
        
        # Bagel config preparing
        config = BagelConfig(
            visual_gen=True,
            visual_und=True,
            llm_config=llm_config, 
            vit_config=vit_config,
            vae_config=vae_config,
            vit_max_num_patch_per_side=70,
            connector_act='gelu_pytorch_tanh',
            latent_patch_size=2,
            max_latent_size=64,
        )
        
        with init_empty_weights():
            language_model = Qwen2ForCausalLM(llm_config)
            vit_model      = SiglipVisionModel(vit_config)
            model          = Bagel(language_model, vit_model, config)
            model.vit_model.vision_model.embeddings.convert_conv2d_to_linear(vit_config, meta=True)
        
        # Tokenizer Preparing
        tokenizer = Qwen2Tokenizer.from_pretrained(model_path)
        tokenizer, new_token_ids, _ = add_special_tokens(tokenizer)
        
        # Image Transform Preparing
        vae_transform = ImageTransform(1024, 512, 16)
        vit_transform = ImageTransform(980, 224, 14)

        max_mem_per_gpu = "40GiB"  # Modify it according to your GPU setting
        
        device_map = infer_auto_device_map(
            model,
            max_memory={i: max_mem_per_gpu for i in range(torch.cuda.device_count())},
            no_split_module_classes=["Bagel", "Qwen2MoTDecoderLayer"],
        )
        print(device_map)
        
        same_device_modules = [
            'language_model.model.embed_tokens',
            'time_embedder',
            'latent_pos_embed',
            'vae2llm',
            'llm2vae',
            'connector',
            'vit_pos_embed'
        ]
        
        if torch.cuda.device_count() == 1:
            first_device = device_map.get(same_device_modules[0], "cuda:0")
            for k in same_device_modules:
                if k in device_map:
                    device_map[k] = first_device
                else:
                    device_map[k] = "cuda:0"
        else:
            first_device = device_map.get(same_device_modules[0])
            for k in same_device_modules:
                if k in device_map:
                    device_map[k] = first_device
        
        # Thanks @onion-liu: https://github.com/ByteDance-Seed/Bagel/pull/8
        model = load_checkpoint_and_dispatch(
            model,
            checkpoint=os.path.join(model_path, "ema.safetensors"),
            device_map=device_map,
            offload_buffers=True,
            dtype=torch.bfloat16,
            force_hooks=True,
        )
        
        model = model.eval()
        print('Model loaded')

        return (model, vae_model, tokenizer, vae_transform, vit_transform, new_token_ids,)


class BagelPrompt:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "text": ("STRING", {
                    "default": "a car made of small cars",
                    "multiline": True
                }),
            }
        }
        
    RETURN_TYPES = ("PROMPT",)
    RETURN_NAMES = ("prompt",)
    FUNCTION = "input_text"
    CATEGORY = "BAGEL"

    def input_text(self, text):
        prompt = text
        return (prompt,)


class LoadEditImage:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "image_path": ("STRING", {"default": "test_images/women.jpg"}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "input_image"
    CATEGORY = "BAGEL"

    def input_image(self, image_path):
        image = image_path
        return (image,)
        

class ImageGeneration:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "vae_model": ("VAEMODEL",),
                "tokenizer": ("TOKENIZER",),
                "vae_transform": ("VAETRANSFORM",),
                "vit_transform": ("VITTRANSFORM",),
                "new_token_ids": ("TOKENIDS",),
                "prompt": ("PROMPT",),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 1000000,
                        "tooltip": "Random seed, 0 for random",
                    },
                ),
                "image_ratio": (
                    ["1:1", "4:3", "3:4", "16:9", "9:16"],
                    {"default": "1:1", "tooltip": "Image aspect ratio"},
                ),
                "cfg_text_scale": (
                    "FLOAT",
                    {
                        "default": 4.0,
                        "min": 1.0,
                        "max": 8.0,
                        "step": 0.1,
                        "tooltip": "CFG text scaling",
                    },
                ),
                "cfg_img_scale": (
                    "FLOAT",
                    {
                        "default": 2.0,
                        "min": 1.0,
                        "max": 4.0,
                        "step": 0.1,
                        "tooltip": "CFG image scaling",
                    },
                ),
                "cfg_interval": (
                    "FLOAT",
                    {
                        "default": 0.4,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.1,
                        "tooltip": "CFG interval start value",
                    },
                ),
                "timestep_shift": (
                    "FLOAT",
                    {
                        "default": 3.0,
                        "min": 1.0,
                        "max": 5.0,
                        "step": 0.5,
                        "tooltip": "Timestep offset",
                    },
                ),
                "num_timesteps": (
                    "INT",
                    {
                        "default": 50,
                        "min": 10,
                        "max": 100,
                        "step": 5,
                        "tooltip": "Denoising steps",
                    },
                ),
                "cfg_renorm_min": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.1,
                        "tooltip": "CFG re-normalization minimum value",
                    },
                ),
                "cfg_renorm_type": (
                    ["global", "local", "text_channel"],
                    {"default": "global", "tooltip": "CFG re-normalization type"},
                ),
                "text_temperature": (
                    "FLOAT",
                    {
                        "default": 0.3,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.1,
                        "tooltip": "Text generation temperature",
                    },
                ),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "generate"
    CATEGORY = "BAGEL"

    def generate(self, model, vae_model, tokenizer, vae_transform, vit_transform, new_token_ids, prompt, image_ratio, 
                 seed, cfg_text_scale, cfg_img_scale, cfg_interval, timestep_shift, num_timesteps, cfg_renorm_min, 
                 cfg_renorm_type, text_temperature):

        inferencer = InterleaveInferencer(
            model=model, 
            vae_model=vae_model, 
            tokenizer=tokenizer, 
            vae_transform=vae_transform, 
            vit_transform=vit_transform, 
            new_token_ids=new_token_ids
        )

        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

        inference_hyper=dict(
            cfg_text_scale=cfg_text_scale,
            cfg_img_scale=cfg_img_scale,
            cfg_interval=[0.4, 1.0],
            timestep_shift=timestep_shift,
            num_timesteps=num_timesteps,
            cfg_renorm_min=cfg_renorm_min,
            cfg_renorm_type=cfg_renorm_type,
        )

        output_dict = inferencer(text=prompt, **inference_hyper)
        image = output_dict['image']
                    
        return (image,)


class ImageThinkGeneration:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "vae_model": ("VAEMODEL",),
                "tokenizer": ("TOKENIZER",),
                "vae_transform": ("VAETRANSFORM",),
                "vit_transform": ("VITTRANSFORM",),
                "new_token_ids": ("TOKENIDS",),
                "prompt": ("PROMPT",),
                "max_think_token_n": ("INT", {"default": 1000}),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 1000000,
                        "tooltip": "Random seed, 0 for random",
                    },
                ),
                "image_ratio": (
                    ["1:1", "4:3", "3:4", "16:9", "9:16"],
                    {"default": "1:1", "tooltip": "Image aspect ratio"},
                ),
                "cfg_text_scale": (
                    "FLOAT",
                    {
                        "default": 4.0,
                        "min": 1.0,
                        "max": 8.0,
                        "step": 0.1,
                        "tooltip": "CFG text scaling",
                    },
                ),
                "cfg_img_scale": (
                    "FLOAT",
                    {
                        "default": 2.0,
                        "min": 1.0,
                        "max": 4.0,
                        "step": 0.1,
                        "tooltip": "CFG image scaling",
                    },
                ),
                "cfg_interval": (
                    "FLOAT",
                    {
                        "default": 0.4,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.1,
                        "tooltip": "CFG interval start value",
                    },
                ),
                "timestep_shift": (
                    "FLOAT",
                    {
                        "default": 3.0,
                        "min": 1.0,
                        "max": 5.0,
                        "step": 0.5,
                        "tooltip": "Timestep offset",
                    },
                ),
                "num_timesteps": (
                    "INT",
                    {
                        "default": 50,
                        "min": 10,
                        "max": 100,
                        "step": 5,
                        "tooltip": "Denoising steps",
                    },
                ),
                "cfg_renorm_min": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.1,
                        "tooltip": "CFG re-normalization minimum value",
                    },
                ),
                "cfg_renorm_type": (
                    ["global", "local", "text_channel"],
                    {"default": "global", "tooltip": "CFG re-normalization type"},
                ),
                "text_temperature": (
                    "FLOAT",
                    {
                        "default": 0.3,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.1,
                        "tooltip": "Text generation temperature",
                    },
                ),
            },
        }

    RETURN_TYPES = ("IMAGE", "TEXT",)
    RETURN_NAMES = ("image", "text",)
    FUNCTION = "generate"
    CATEGORY = "BAGEL"

    def generate(self, model, vae_model, tokenizer, vae_transform, vit_transform, new_token_ids, prompt, 
                seed, max_think_token_n, cfg_text_scale, cfg_img_scale, timestep_shift, num_timesteps, cfg_renorm_min, 
                cfg_interval, cfg_renorm_type, text_temperature):

        inferencer = InterleaveInferencer(
            model=model, 
            vae_model=vae_model, 
            tokenizer=tokenizer, 
            vae_transform=vae_transform, 
            vit_transform=vit_transform, 
            new_token_ids=new_token_ids
        )

        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

        inference_hyper=dict(
            max_think_token_n=max_think_token_n,
            do_sample=False,
            # text_temperature=0.3,
            cfg_text_scale=cfg_text_scale,
            cfg_img_scale=cfg_img_scale,
            cfg_interval=[0.4, 1.0],
            timestep_shift=timestep_shift,
            num_timesteps=num_timesteps,
            cfg_renorm_min=cfg_renorm_min,
            cfg_renorm_type="global",
        )
        
        output_dict = inferencer(text=prompt, think=True, **inference_hyper)
        image = output_dict['image']
        text = output_dict['text']
                    
        return (image, text)


class ImageEditing:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "vae_model": ("VAEMODEL",),
                "tokenizer": ("TOKENIZER",),
                "vae_transform": ("VAETRANSFORM",),
                "vit_transform": ("VITTRANSFORM",),
                "new_token_ids": ("TOKENIDS",),
                "prompt": ("PROMPT",),
                "image": ("IMAGE",),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 1000000,
                        "tooltip": "Random seed, 0 for random",
                    },
                ),
                "image_ratio": (
                    ["1:1", "4:3", "3:4", "16:9", "9:16"],
                    {"default": "1:1", "tooltip": "Image aspect ratio"},
                ),
                "cfg_text_scale": (
                    "FLOAT",
                    {
                        "default": 4.0,
                        "min": 1.0,
                        "max": 8.0,
                        "step": 0.1,
                        "tooltip": "CFG text scaling",
                    },
                ),
                "cfg_img_scale": (
                    "FLOAT",
                    {
                        "default": 2.0,
                        "min": 1.0,
                        "max": 4.0,
                        "step": 0.1,
                        "tooltip": "CFG image scaling",
                    },
                ),
                "cfg_interval": (
                    "FLOAT",
                    {
                        "default": 0.4,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.1,
                        "tooltip": "CFG interval start value",
                    },
                ),
                "timestep_shift": (
                    "FLOAT",
                    {
                        "default": 3.0,
                        "min": 1.0,
                        "max": 5.0,
                        "step": 0.5,
                        "tooltip": "Timestep offset",
                    },
                ),
                "num_timesteps": (
                    "INT",
                    {
                        "default": 50,
                        "min": 10,
                        "max": 100,
                        "step": 5,
                        "tooltip": "Denoising steps",
                    },
                ),
                "cfg_renorm_min": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.1,
                        "tooltip": "CFG re-normalization minimum value",
                    },
                ),
                "cfg_renorm_type": (
                    ["global", "local", "text_channel"],
                    {"default": "global", "tooltip": "CFG re-normalization type"},
                ),
                "text_temperature": (
                    "FLOAT",
                    {
                        "default": 0.3,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.1,
                        "tooltip": "Text generation temperature",
                    },
                ),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "editing"
    CATEGORY = "BAGEL"

    def editing(self, model, vae_model, tokenizer, vae_transform, vit_transform, new_token_ids, prompt, 
                image, seed, cfg_text_scale, cfg_img_scale, timestep_shift, num_timesteps, cfg_renorm_min):

        inferencer = InterleaveInferencer(
            model=model, 
            vae_model=vae_model, 
            tokenizer=tokenizer, 
            vae_transform=vae_transform, 
            vit_transform=vit_transform, 
            new_token_ids=new_token_ids
        )

        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

        inference_hyper=dict(
            cfg_text_scale=cfg_text_scale,
            cfg_img_scale=cfg_img_scale,
            cfg_interval=[0.0, 1.0],
            timestep_shift=timestep_shift,
            num_timesteps=num_timesteps,
            cfg_renorm_min=cfg_renorm_min,
            cfg_renorm_type="text_channel",
        )
        
        output_dict = inferencer(image=image, text=prompt, **inference_hyper)
        image = output_dict['image']
                    
        return (image,)


class ImageThinkEditing:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "vae_model": ("VAEMODEL",),
                "tokenizer": ("TOKENIZER",),
                "vae_transform": ("VAETRANSFORM",),
                "vit_transform": ("VITTRANSFORM",),
                "new_token_ids": ("TOKENIDS",),
                "prompt": ("PROMPT",),
                "image": ("IMAGE",),
                "max_think_token_n": ("INT", {"default": 1000}),
                "seed": (
                    "INT",
                    {
                        "default": 42,
                        "min": 0,
                        "max": 1000000,
                        "tooltip": "Random seed, 0 for random",
                    },
                ),
                "image_ratio": (
                    ["1:1", "4:3", "3:4", "16:9", "9:16"],
                    {"default": "1:1", "tooltip": "Image aspect ratio"},
                ),
                "cfg_text_scale": (
                    "FLOAT",
                    {
                        "default": 4.0,
                        "min": 1.0,
                        "max": 8.0,
                        "step": 0.1,
                        "tooltip": "CFG text scaling",
                    },
                ),
                "cfg_img_scale": (
                    "FLOAT",
                    {
                        "default": 2.0,
                        "min": 1.0,
                        "max": 4.0,
                        "step": 0.1,
                        "tooltip": "CFG image scaling",
                    },
                ),
                "cfg_interval": (
                    "FLOAT",
                    {
                        "default": 0.4,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.1,
                        "tooltip": "CFG interval start value",
                    },
                ),
                "timestep_shift": (
                    "FLOAT",
                    {
                        "default": 3.0,
                        "min": 1.0,
                        "max": 5.0,
                        "step": 0.5,
                        "tooltip": "Timestep offset",
                    },
                ),
                "num_timesteps": (
                    "INT",
                    {
                        "default": 50,
                        "min": 10,
                        "max": 100,
                        "step": 5,
                        "tooltip": "Denoising steps",
                    },
                ),
                "cfg_renorm_min": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.1,
                        "tooltip": "CFG re-normalization minimum value",
                    },
                ),
                "cfg_renorm_type": (
                    ["global", "local", "text_channel"],
                    {"default": "global", "tooltip": "CFG re-normalization type"},
                ),
                "text_temperature": (
                    "FLOAT",
                    {
                        "default": 0.3,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.1,
                        "tooltip": "Text generation temperature",
                    },
                ),
            }
        }

    RETURN_TYPES = ("IMAGE", "TEXT",)
    RETURN_NAMES = ("image", "text",)
    FUNCTION = "editing"
    CATEGORY = "BAGEL"

    def editing(self, model, vae_model, tokenizer, vae_transform, vit_transform, new_token_ids, prompt, 
                image, seed, max_think_token_n, cfg_text_scale, cfg_img_scale, timestep_shift, num_timesteps, cfg_renorm_min):

        inferencer = InterleaveInferencer(
            model=model, 
            vae_model=vae_model, 
            tokenizer=tokenizer, 
            vae_transform=vae_transform, 
            vit_transform=vit_transform, 
            new_token_ids=new_token_ids
        )

        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

        inference_hyper=dict(
            max_think_token_n=max_think_token_n,
            do_sample=False,
            # text_temperature=0.3,
            cfg_text_scale=cfg_text_scale,
            cfg_img_scale=cfg_img_scale,
            cfg_interval=[0.0, 1.0],
            timestep_shift=timestep_shift,
            num_timesteps=num_timesteps,
            cfg_renorm_min=cfg_renorm_min,
            cfg_renorm_type="text_channel",
        )

        output_dict = inferencer(image=image, text=prompt, think=True, **inference_hyper)
        image = output_dict['image']
        text = output_dict['text']
                    
        return (image, text)


class ImageUnderstanding:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL",),
                "vae_model": ("VAEMODEL",),
                "tokenizer": ("TOKENIZER",),
                "vae_transform": ("VAETRANSFORM",),
                "vit_transform": ("VITTRANSFORM",),
                "new_token_ids": ("TOKENIDS",),
                "prompt": ("PROMPT",),
                "image": ("IMAGE",),
                "seed": ("INT", {"default": 42}),
                "max_think_token_n": ("INT", {"default": 1000}),
            }
        }

    RETURN_TYPES = ("TEXT",)
    RETURN_NAMES = ("text",)
    FUNCTION = "understanding"
    CATEGORY = "BAGEL"

    def understanding(self, model, vae_model, tokenizer, vae_transform, vit_transform, new_token_ids, prompt, 
                image, seed, max_think_token_n):

        inferencer = InterleaveInferencer(
            model=model, 
            vae_model=vae_model, 
            tokenizer=tokenizer, 
            vae_transform=vae_transform, 
            vit_transform=vit_transform, 
            new_token_ids=new_token_ids
        )

        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

        inference_hyper=dict(
            max_think_token_n=1000,
            do_sample=False,
            # text_temperature=0.3,
        )

        output_dict = inferencer(image=image, text=prompt, understanding_output=True, **inference_hyper)
        text = output_dict['text']
                    
        return (text,)

