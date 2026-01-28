import argparse
import json
import os
import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call octen-embedding-8b via the LiteLLM proxy."
    )
    parser.add_argument(
        "--input",
        default=os.environ.get("EMBEDDING_INPUT", "Hello from octen"),
        help="Text input to embed (default: EMBEDDING_INPUT or 'Hello from octen').",
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
        default=os.environ.get("MODEL", "octen-embedding-8b"),
        help="Model name (default: octen-embedding-8b or MODEL).",
    )
    parser.add_argument(
        "--encoding-format",
        default=os.environ.get("ENCODING_FORMAT", "float"),
        help="Encoding format (default: float or ENCODING_FORMAT).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.api_key:
        raise SystemExit(
            "Missing LITELLM_API_KEY/LITELLM_MASTER_KEY (or --api-key)."
        )

    resp = requests.post(
        f"{args.api_base}/v1/embeddings",
        headers={"Authorization": f"Bearer {args.api_key}"},
        json={
            "model": args.model,
            "input": args.input,
            "encoding_format": args.encoding_format,
        },
        timeout=120,
    )

    resp.raise_for_status()
    print(json.dumps(resp.json(), indent=2))


if __name__ == "__main__":
    main()
