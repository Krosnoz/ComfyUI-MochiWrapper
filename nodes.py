import os
# import torch._dynamo
# torch._dynamo.config.suppress_errors = True

import torch
import folder_paths
import comfy.model_management as mm
from comfy.utils import ProgressBar, load_torch_file
from einops import rearrange
from tqdm import tqdm
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

from .mochi_preview.t2v_synth_mochi import T2VSynthMochiModel
from .mochi_preview.vae.model import Decoder

from contextlib import nullcontext
try:
    from accelerate import init_empty_weights
    from accelerate.utils import set_module_tensor_to_device
    is_accelerate_available = True
except:
    is_accelerate_available = False
    pass

script_directory = os.path.dirname(os.path.abspath(__file__))

def linear_quadratic_schedule(num_steps, threshold_noise, linear_steps=None):
    if linear_steps is None:
        linear_steps = num_steps // 2
    linear_sigma_schedule = [i * threshold_noise / linear_steps for i in range(linear_steps)]
    threshold_noise_step_diff = linear_steps - threshold_noise * num_steps
    quadratic_steps = num_steps - linear_steps
    quadratic_coef = threshold_noise_step_diff / (linear_steps * quadratic_steps ** 2)
    linear_coef = threshold_noise / linear_steps - 2 * threshold_noise_step_diff / (quadratic_steps ** 2)
    const = quadratic_coef * (linear_steps ** 2)
    quadratic_sigma_schedule = [
        quadratic_coef * (i ** 2) + linear_coef * i + const
        for i in range(linear_steps, num_steps)
    ]
    sigma_schedule = linear_sigma_schedule + quadratic_sigma_schedule + [1.0]
    sigma_schedule = [1.0 - x for x in sigma_schedule]
    return sigma_schedule
   
class DownloadAndLoadMochiModel:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": (
                    [   
                        "mochi_preview_dit_fp8_e4m3fn.safetensors",
                        "mochi_preview_dit_bf16.safetensors",
                        "mochi_preview_dit_GGUF_Q4_0_v2.safetensors",
                        "mochi_preview_dit_GGUF_Q8_0.safetensors",

                    ],
                    {"tooltip": "Downloads from 'https://huggingface.co/Kijai/Mochi_preview_comfy' to 'models/diffusion_models/mochi'", },
                ),
                "vae": (
                    [   
                        "mochi_preview_vae_bf16.safetensors",
                    ],
                    {"tooltip": "Downloads from 'https://huggingface.co/Kijai/Mochi_preview_comfy' to 'models/vae/mochi'", },
                ),
                 "precision": (["bf16", "fp8_e4m3fn", "fp8_e4m3fn_fast", "fp16", "fp32"],
                    {"default": "bf16"}),
                "attention_mode": (["flash_attn", "sdpa", "sage_attn", "comfy"],
                    {"default": "flash_attn"}),
            },
            "optional": {
                "trigger": ("CONDITIONING", {"tooltip": "Dummy input for forcing execution order",}),
                "compile_args": ("MOCHICOMPILEARGS", {"tooltip": "Optional torch.compile arguments",}),
            },
        }

    RETURN_TYPES = ("MOCHIMODEL", "MOCHIVAE",)
    RETURN_NAMES = ("mochi_model", "mochi_vae" )
    FUNCTION = "loadmodel"
    CATEGORY = "MochiWrapper"
    DESCRIPTION = "Downloads and loads the selected Mochi model from Huggingface"

    def loadmodel(self, model, vae, precision, attention_mode, trigger=None, compile_args=None):

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        mm.soft_empty_cache()

        dtype = {"fp8_e4m3fn": torch.float8_e4m3fn, "fp8_e4m3fn_fast": torch.float8_e4m3fn, "bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]

        # Transformer model
        model_download_path = os.path.join(folder_paths.models_dir, 'diffusion_models', 'mochi')
        model_path = os.path.join(model_download_path, model)
   
        repo_id = "kijai/Mochi_preview_comfy"
        
        if not os.path.exists(model_path):
            log.info(f"Downloading mochi model to: {model_path}")
            from huggingface_hub import snapshot_download
            snapshot_download(
                repo_id=repo_id,
                allow_patterns=[f"*{model}*"],
                local_dir=model_download_path,
                local_dir_use_symlinks=False,
            )
        # VAE
        vae_download_path = os.path.join(folder_paths.models_dir, 'vae', 'mochi')
        vae_path = os.path.join(vae_download_path, vae)

        if not os.path.exists(vae_path):
            log.info(f"Downloading mochi VAE to: {vae_path}")
            from huggingface_hub import snapshot_download
            snapshot_download(
                repo_id=repo_id,
                allow_patterns=[f"*{vae}*"],
                local_dir=vae_download_path,
                local_dir_use_symlinks=False,
            )

        model = T2VSynthMochiModel(
            device=device,
            offload_device=offload_device,
            vae_stats_path=os.path.join(script_directory, "configs", "vae_stats.json"),
            dit_checkpoint_path=model_path,
            weight_dtype=dtype,
            fp8_fastmode = True if precision == "fp8_e4m3fn_fast" else False,
            attention_mode=attention_mode,
            compile_args=compile_args
        )
        with (init_empty_weights() if is_accelerate_available else nullcontext()):
            vae = Decoder(
                    out_channels=3,
                    base_channels=128,
                    channel_multipliers=[1, 2, 4, 6],
                    temporal_expansions=[1, 2, 3],
                    spatial_expansions=[2, 2, 2],
                    num_res_blocks=[3, 3, 4, 6, 3],
                    latent_dim=12,
                    has_attention=[False, False, False, False, False],
                    padding_mode="replicate",
                    output_norm=False,
                    nonlinearity="silu",
                    output_nonlinearity="silu",
                    causal=True,
                )
        vae_sd = load_torch_file(vae_path)
        if is_accelerate_available:
            for key in vae_sd:
                set_module_tensor_to_device(vae, key, dtype=torch.float32, device=device, value=vae_sd[key])
        else:
            vae.load_state_dict(vae_sd, strict=True)
            vae.eval().to(torch.bfloat16).to(device)
        del vae_sd

        return (model, vae,)
    
class MochiModelLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": { 
                "model_name": (folder_paths.get_filename_list("diffusion_models"), {"tooltip": "The name of the checkpoint (model) to load.",}),
                "precision": (["fp8_e4m3fn","fp8_e4m3fn_fast","fp16", "fp32", "bf16"], {"default": "fp8_e4m3fn"}),
                "attention_mode": (["sdpa","flash_attn","sage_attn", "comfy"],),
            },
            "optional": {
                "trigger": ("CONDITIONING", {"tooltip": "Dummy input for forcing execution order",}),
                "compile_args": ("MOCHICOMPILEARGS", {"tooltip": "Optional torch.compile arguments",}),
            },
        }
    RETURN_TYPES = ("MOCHIMODEL",)
    RETURN_NAMES = ("mochi_model",)
    FUNCTION = "loadmodel"
    CATEGORY = "MochiWrapper"

    def loadmodel(self, model_name, precision, attention_mode, trigger=None, compile_args=None):

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        mm.soft_empty_cache()

        dtype = {"fp8_e4m3fn": torch.float8_e4m3fn, "fp8_e4m3fn_fast": torch.float8_e4m3fn, "bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]
        model_path = folder_paths.get_full_path_or_raise("diffusion_models", model_name)

        model = T2VSynthMochiModel(
            device=device,
            offload_device=offload_device,
            vae_stats_path=os.path.join(script_directory, "configs", "vae_stats.json"),
            dit_checkpoint_path=model_path,
            weight_dtype=dtype,
            fp8_fastmode = True if precision == "fp8_e4m3fn_fast" else False,
            attention_mode=attention_mode,
            compile_args=compile_args
        )

        # Optimisation du format mémoire
        model = self.optimize_memory_format(model)
        
        # Compilation du modèle
        if compile_args is None:
            compile_args = ModelConfig().compile_args
        model = torch.compile(model, **compile_args)

        return (model, )

    def optimize_memory_format(self, model):
        return model.to(memory_format=torch.channels_last)

    def load_weights(self, path):
        torch.cuda.empty_cache()
        state_dict = torch.load(path, map_location='cpu')
        state_dict = {k: v.to(torch.bfloat16) for k, v in state_dict.items()}
        prefetch = {k: v.cuda() for k, v in state_dict.items()}
        return prefetch
        
class MochiTorchCompileSettings:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": { 
                "backend": (["inductor","cudagraphs"], {"default": "inductor"}),
                "fullgraph": ("BOOLEAN", {"default": False, "tooltip": "Enable full graph mode"}),
                "mode": (["default", "max-autotune", "max-autotune-no-cudagraphs", "reduce-overhead"], {"default": "default"}),
                "compile_dit": ("BOOLEAN", {"default": True, "tooltip": "Compiles all transformer blocks"}),
                "compile_final_layer": ("BOOLEAN", {"default": True, "tooltip": "Enable compiling final layer."}),
            },
        }
    RETURN_TYPES = ("MOCHICOMPILEARGS",)
    RETURN_NAMES = ("torch_compile_args",)
    FUNCTION = "loadmodel"
    CATEGORY = "MochiWrapper"
    DESCRIPTION = "torch.compile settings, when connected to the model loader, torch.compile of the selected layers is attempted. Requires Triton and torch 2.5.0 is recommended"

    def loadmodel(self, backend, fullgraph, mode, compile_dit, compile_final_layer):

        compile_args = {
            "backend": backend,
            "fullgraph": fullgraph,
            "mode": mode,
            "compile_dit": compile_dit,
            "compile_final_layer": compile_final_layer,
        }

        return (compile_args, )
    
class MochiVAELoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": { 
                "model_name": (folder_paths.get_filename_list("vae"), {"tooltip": "The name of the checkpoint (vae) to load."}),
            },
            "optional": {
                "torch_compile_args": ("MOCHICOMPILEARGS", {"tooltip": "Optional torch.compile arguments",}),
            },
        }

    RETURN_TYPES = ("MOCHIVAE",)
    RETURN_NAMES = ("mochi_vae", )
    FUNCTION = "loadmodel"
    CATEGORY = "MochiWrapper"

    def loadmodel(self, model_name, torch_compile_args=None):

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        mm.soft_empty_cache()

        vae_path = folder_paths.get_full_path_or_raise("vae", model_name)

        with (init_empty_weights() if is_accelerate_available else nullcontext()):
            vae = Decoder(
                    out_channels=3,
                    base_channels=128,
                    channel_multipliers=[1, 2, 4, 6],
                    temporal_expansions=[1, 2, 3],
                    spatial_expansions=[2, 2, 2],
                    num_res_blocks=[3, 3, 4, 6, 3],
                    latent_dim=12,
                    has_attention=[False, False, False, False, False],
                    padding_mode="replicate",
                    output_norm=False,
                    nonlinearity="silu",
                    output_nonlinearity="silu",
                    causal=True,
                )
        vae_sd = load_torch_file(vae_path)
        if is_accelerate_available:
            for key in vae_sd:
                set_module_tensor_to_device(vae, key, dtype=torch.float32, device=offload_device, value=vae_sd[key])
        else:
            vae.load_state_dict(vae_sd, strict=True)
            vae.to(torch.bfloat16).to("cpu")
        vae.eval()
        del vae_sd

        if torch_compile_args is not None:
            vae.to(device)
            # for i, block in enumerate(vae.blocks):
            #     if "CausalUpsampleBlock" in str(type(block)): 
            #         print("Compiling block", block)
            vae = torch.compile(vae, fullgraph=torch_compile_args["fullgraph"], mode=torch_compile_args["mode"], dynamic=False, backend=torch_compile_args["backend"])
    

        return (vae,)
    
class MochiTextEncode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "clip": ("CLIP",),
            "prompt": ("STRING", {"default": "", "multiline": True} ),
            },
            "optional": {
                "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "force_offload": ("BOOLEAN", {"default": True}),
            }
        }

    RETURN_TYPES = ("CONDITIONING", "CLIP",)
    RETURN_NAMES = ("conditioning", "clip", )
    FUNCTION = "process"
    CATEGORY = "MochiWrapper"

    def process(self, clip, prompt, strength=1.0, force_offload=True):
        max_tokens = 256
        load_device = mm.text_encoder_device()
        offload_device = mm.text_encoder_offload_device()

        clip.tokenizer.t5xxl.pad_to_max_length = True
        clip.tokenizer.t5xxl.max_length = max_tokens
        clip.cond_stage_model.t5xxl.return_attention_masks = True
        clip.cond_stage_model.t5xxl.enable_attention_masks = True
        clip.cond_stage_model.t5_attention_mask = True
        clip.cond_stage_model.to(load_device)
        tokens = clip.tokenizer.t5xxl.tokenize_with_weights(prompt, return_word_ids=True)
        
        try:
            embeds, _, attention_mask = clip.cond_stage_model.t5xxl.encode_token_weights(tokens)
        except:
            NotImplementedError("Failed to get attention mask from T5, is your ComfyUI up to date?")

        if embeds.shape[1] > 256:
            raise ValueError(f"Prompt is too long, max tokens supported is {max_tokens} or less, got {embeds.shape[1]}")
        embeds *= strength
        if force_offload:
            clip.cond_stage_model.to(offload_device)

        t5_embeds = {
            "embeds": embeds,
            "attention_mask": attention_mask["attention_mask"].bool(),
        }
        return (t5_embeds, clip,)
        
class MochiImageEncode:
    @classmethod 
    def INPUT_TYPES(s):
        return {"required": {
            "image": ("IMAGE",),
            "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
            },
        }

    RETURN_TYPES = ("CONDITIONING",)
    RETURN_NAMES = ("conditioning",)
    FUNCTION = "encode"
    CATEGORY = "MochiWrapper"

    def encode(self, image, strength=1.0):
        # Convertir l'image en tenseur et normaliser
        if len(image.shape) == 3:
            image = image.unsqueeze(0)
        
        # Redimensionner si nécessaire pour correspondre aux attentes du modèle
        image = torch.nn.functional.interpolate(image, size=(224, 224), mode='bilinear')
        
        # Normaliser l'image
        image = (image - 0.5) * 2
        
        # Encoder l'image
        image_embeds = image * strength
        
        # Créer un masque d'attention (tout à True car nous voulons utiliser toute l'image)
        attention_mask = torch.ones(image_embeds.shape[0], device=image_embeds.device, dtype=torch.bool)

        return ({"embeds": image_embeds, "attention_mask": attention_mask},)

class MochiSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MOCHIMODEL",),
                "positive": ("CONDITIONING", ),
                "negative": ("CONDITIONING", ),
                "width": ("INT", {"default": 848, "min": 128, "max": 2048, "step": 8}),
                "height": ("INT", {"default": 480, "min": 128, "max": 2048, "step": 8}),
                "num_frames": ("INT", {"default": 49, "min": 7, "max": 1024, "step": 6}),
                "steps": ("INT", {"default": 50, "min": 2}),
                "cfg": ("FLOAT", {"default": 4.5, "min": 0.0, "max": 30.0, "step": 0.01}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            },
            "optional": {
                "image_cond": ("CONDITIONING",),
                "image_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
            }
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("model", "samples",)
    FUNCTION = "process"
    CATEGORY = "MochiWrapper"

    def process(self, model, positive, negative, steps, cfg, seed, height, width, num_frames, image_cond=None, image_strength=1.0):
        mm.soft_empty_cache()

        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        args = {
            "height": height,
            "width": width,
            "num_frames": num_frames,
            "mochi_args": {
                "sigma_schedule": linear_quadratic_schedule(steps, 0.025),
                "cfg_schedule": [cfg] * steps,
                "num_inference_steps": steps,
                "batch_cfg": False,
            },
            "positive_embeds": positive,
            "negative_embeds": negative,
            "seed": seed,
        }
        if image_cond is not None:
            # Combiner le conditionnement texte et image
            combined_embeds = torch.cat([
                positive["embeds"],
                image_cond["embeds"] * image_strength
            ], dim=1)
            
            combined_mask = torch.cat([
                positive["attention_mask"],
                image_cond["attention_mask"]
            ], dim=1)
            
            args["positive_embeds"] = {
                "embeds": combined_embeds,
                "attention_mask": combined_mask
            }
        
        latents = model.run(args)
    
        mm.soft_empty_cache()

        return ({"samples": latents},)
    
class MochiDecode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "vae": ("MOCHIVAE",),
            "samples": ("LATENT", ),
            "enable_vae_tiling": ("BOOLEAN", {"default": False, "tooltip": "Drastically reduces memory use but may introduce seams"}),
            "auto_tile_size": ("BOOLEAN", {"default": True, "tooltip": "Auto size based on height and width, default is half the size"}),
            "frame_batch_size": ("INT", {"default": 6, "min": 1, "max": 64, "step": 1, "tooltip": "Number of frames in latent space (downscale factor is 6) to decode at once"}),
            "tile_sample_min_height": ("INT", {"default": 240, "min": 16, "max": 2048, "step": 8, "tooltip": "Minimum tile height, default is half the height"}),
            "tile_sample_min_width": ("INT", {"default": 424, "min": 16, "max": 2048, "step": 8, "tooltip": "Minimum tile width, default is half the width"}),
            "tile_overlap_factor_height": ("FLOAT", {"default": 0.1666, "min": 0.0, "max": 1.0, "step": 0.001}),
            "tile_overlap_factor_width": ("FLOAT", {"default": 0.2, "min": 0.0, "max": 1.0, "step": 0.001}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "decode"
    CATEGORY = "MochiWrapper"

    def decode(self, vae, samples, enable_vae_tiling, tile_sample_min_height, tile_sample_min_width, 
               tile_overlap_factor_height, tile_overlap_factor_width, auto_tile_size, frame_batch_size):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        
        # Optimisé pour bf16
        samples = samples["samples"].to(torch.bfloat16)
        
        # Augmenter la taille du batch pour A40
        if device == "cuda" and torch.cuda.get_device_properties(0).total_memory >= 48 * 1024 * 1024 * 1024:
            frame_batch_size = min(frame_batch_size * 2, samples.shape[2])
        
        # Paramètres de tiling optimisés pour A40
        if auto_tile_size:
            self.tile_overlap_factor_height = 0.1
            self.tile_overlap_factor_width = 0.1
            self.tile_sample_min_height = samples.shape[3] // 3
            self.tile_sample_min_width = samples.shape[4] // 3
        
        # Reste du code avec context manager bf16
        with torch.cuda.amp.autocast(dtype=torch.bfloat16):
            if enable_vae_tiling:
                frames = decode_tiled(samples)
            else:
                frames = vae(samples)
        
        # Conversion finale en float32
        frames = frames.float()
        frames = (frames + 1.0) / 2.0
        frames.clamp_(0.0, 1.0)
        
        return (frames,)

class MochiDecodeSpatialTiling:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "vae": ("MOCHIVAE",),
            "samples": ("LATENT", ),
            "enable_vae_tiling": ("BOOLEAN", {"default": False, "tooltip": "Drastically reduces memory use but may introduce seams"}),
            "num_tiles_w": ("INT", {"default": 4, "min": 2, "max": 64, "step": 2, "tooltip": "Number of horizontal tiles"}),
            "num_tiles_h": ("INT", {"default": 4, "min": 2, "max": 64, "step": 2, "tooltip": "Number of vertical tiles"}),
            "overlap": ("INT", {"default": 16, "min": 0, "max": 256, "step": 1, "tooltip": "Number of pixel of overlap between adjacent tiles"}),
            "min_block_size": ("INT", {"default": 1, "min": 1, "max": 256, "step": 1, "tooltip": "Minimum number of pixels in each dimension when subdividing"}),
            "per_batch": ("INT", {"default": 6, "min": 1, "max": 256, "step": 1, "tooltip": "Number of samples per batch, in latent space (6 frames in 1 latent)"}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "decode"
    CATEGORY = "MochiWrapper"

    def decode(self, vae, samples, enable_vae_tiling, num_tiles_w, num_tiles_h, overlap, 
               min_block_size, per_batch):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        intermediate_device = mm.intermediate_device()
        samples = samples["samples"]
        samples = samples.to(torch.bfloat16).to(device)

        B, C, T, H, W = samples.shape

        vae.to(device)
        decoded_list = []
        with torch.autocast(mm.get_autocast_device(device), dtype=torch.bfloat16):
            if enable_vae_tiling:
                from .mochi_preview.vae.model import apply_tiled
                logging.warning("Decoding with tiling...")
                for i in range(0, T, per_batch):
                    end_index = min(i + per_batch, T)
                    chunk = samples[:, :, i:end_index, :, :] 
                    frames = apply_tiled(vae, chunk, num_tiles_w = num_tiles_w, num_tiles_h = num_tiles_h, overlap=overlap, min_block_size=min_block_size)
                    print(frames.shape)
                     # Blend the first and last frames of each pair
                    if len(decoded_list) > 0:
                        previous_frames = decoded_list[-1]
                        blended_frames = (previous_frames[:, :, -1:, :, :] + frames[:, :, :1, :, :]) / 2
                        decoded_list[-1][:, :, -1:, :, :] = blended_frames
                    
                    decoded_list.append(frames)
            else:
                logging.info("Decoding without tiling...")
                frames = vae(samples)
        frames = torch.cat(decoded_list, dim=2)
        vae.to(offload_device)

        frames = frames.float()
        frames = (frames + 1.0) / 2.0
        frames.clamp_(0.0, 1.0)

        frames = rearrange(frames, "b c t h w -> (t b) h w c").to(intermediate_device)

        return (frames,)


NODE_CLASS_MAPPINGS = {
    "DownloadAndLoadMochiModel": DownloadAndLoadMochiModel,
    "MochiSampler": MochiSampler,
    "MochiDecode": MochiDecode,
    "MochiTextEncode": MochiTextEncode,
    "MochiModelLoader": MochiModelLoader,
    "MochiVAELoader": MochiVAELoader,
    "MochiDecodeSpatialTiling": MochiDecodeSpatialTiling,
    "MochiTorchCompileSettings": MochiTorchCompileSettings
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DownloadAndLoadMochiModel": "(Down)load Mochi Model",
    "MochiSampler": "Mochi Sampler",
    "MochiDecode": "Mochi Decode",
    "MochiTextEncode": "Mochi TextEncode",
    "MochiModelLoader": "Mochi Model Loader",
    "MochiVAELoader": "Mochi VAE Loader",
    "MochiDecodeSpatialTiling": "Mochi VAE Decode Spatial Tiling",
    "MochiTorchCompileSettings": "Mochi Torch Compile Settings"
    }

NODE_CLASS_MAPPINGS.update({
    "MochiImageEncode": MochiImageEncode,
})

NODE_DISPLAY_NAME_MAPPINGS.update({
    "MochiImageEncode": "Mochi Image Encode",
})


