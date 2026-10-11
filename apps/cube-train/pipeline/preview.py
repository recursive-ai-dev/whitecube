"""Small CPU checkpoint preview, producing only generated text on stdout."""
import argparse
import contextlib
import sys
import math


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--max-new-tokens', type=int, default=120)
    parser.add_argument('--temperature', type=float, default=.85)
    parser.add_argument('--top-k', type=int, default=40)
    parser.add_argument('--seed', type=int, default=1337)
    args = parser.parse_args()
    if not 1 <= args.max_new_tokens <= 4096:
        parser.error('--max-new-tokens must be between 1 and 4096')
    if not math.isfinite(args.temperature) or args.temperature < 0:
        parser.error('--temperature must be finite and nonnegative')
    if args.top_k < 1:
        parser.error('--top-k must be positive')
    # Model constructors report their shape; keep that in diagnostics.
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from tensegrity_lm import TensegrityConfig, TensegrityLM, load_checkpoint
        from tensegrity_serve import decode
        torch.set_num_threads(2)
        torch.manual_seed(args.seed)
        saved = load_checkpoint(args.checkpoint, 'cpu')
        cfg = TensegrityConfig(**saved['config'])
        model = TensegrityLM(cfg).eval()
        model.load_state_dict(saved['model_state_dict'])
    prompt = list(args.prompt.encode('utf-8')[-cfg.block_size:]) or [256]
    tokens = list(decode(model, prompt, temperature=args.temperature, top_k=args.top_k, top_p=None,
                         max_new_tokens=args.max_new_tokens))
    print(bytes(token for token in tokens if token < 256).decode('utf-8', errors='replace'))


if __name__ == '__main__':
    main()
