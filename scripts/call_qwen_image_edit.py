import argparse
import base64
import os
import requests

DEFAULT_PROMPT = (
    "Make the garment sleeveless with clean armholes while preserving color, texture, "
    "pattern, lighting, background, and pose."
)
DEFAULT_NEGATIVE_PROMPT = (
    "blurry, artifacts, color shift, lighting change, background change, pose change, "
    "body shape change, garment change, warped fabric, texture loss"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call the qwen-image-edit model via the LiteLLM server."
    )
    parser.add_argument(
        "image",
        nargs="?",
        default=os.environ.get("IMAGE_PATH", "test_image.jpg"),
        help="Path to the input image (default: test_image.jpg or IMAGE_PATH).",
    )
    parser.add_argument(
        "--prompt",
        default=os.environ.get("PROMPT", DEFAULT_PROMPT),
        help="Edit prompt (default: PROMPT env or built-in).",
    )
    parser.add_argument(
        "--negative-prompt",
        default=os.environ.get("NEGATIVE_PROMPT", DEFAULT_NEGATIVE_PROMPT),
        help="Negative prompt (default: NEGATIVE_PROMPT env or built-in).",
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("API_BASE", "http://litellm-service:4000"),
        help="LiteLLM base URL (default: http://litellm-service:4000 or API_BASE).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("LITELLM_API_KEY")
        or os.environ.get("LITELLM_MASTER_KEY"),
        help="LiteLLM API key (default: LITELLM_API_KEY or LITELLM_MASTER_KEY).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("MODEL", "qwen-image-edit"),
        help="Model name (default: qwen-image-edit or MODEL).",
    )
    parser.add_argument(
        "--out",
        default=os.environ.get("OUTPUT_PATH", "edited.png"),
        help="Output image path (default: edited.png or OUTPUT_PATH).",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=int(os.environ.get("INFERENCE_STEPS", "50")),
        help="Number of inference steps (default: 50 or INFERENCE_STEPS).",
    )
    parser.add_argument(
        "--cfg-scale",
        type=float,
        default=float(os.environ.get("CFG_SCALE", "10.0")),
        help="CFG scale (default: 10.0 or CFG_SCALE).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=int(os.environ.get("SEED", "42")),
        help="Random seed (default: 42 or SEED).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.api_key:
        raise SystemExit(
            "Missing LITELLM_API_KEY/LITELLM_MASTER_KEY (or --api-key)."
        )

    with open(args.image, "rb") as f:
        resp = requests.post(
            f"{args.api_base}/v1/images/edits",
            headers={"Authorization": f"Bearer {args.api_key}"},
            files={"image": f},
            data={
                "model": args.model,
                "prompt": args.prompt,
                "num_inference_steps": str(args.steps),
                "true_cfg_scale": str(args.cfg_scale),
                "seed": str(args.seed),
                "negative_prompt": args.negative_prompt,
            },
            timeout=120,
        )

    resp.raise_for_status()
    data = resp.json()["data"][0]["b64_json"]
    with open(args.out, "wb") as f:
        f.write(base64.b64decode(data))
    print(f"saved {args.out}")


if __name__ == "__main__":
    main()
