"""Small CPU checkpoint preview, producing only generated text on stdout."""
import argparse
import contextlib
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--prompt', required=True)
    args = parser.parse_args()
    # Model constructors report their shape; keep that in diagnostics.
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from tensegrity_lm import TensegrityConfig, TensegrityLM, load_checkpoint
        from tensegrity_serve import decode
        torch.set_num_threads(2)
        saved = load_checkpoint(args.checkpoint, 'cpu')
        cfg = TensegrityConfig(**saved['config'])
        model = TensegrityLM(cfg).eval()
        model.load_state_dict(saved['model_state_dict'])
    prompt = list(args.prompt.encode('utf-8')[-max(32, cfg.block_size // 2):])
    tokens = list(decode(model, prompt, temperature=.85, top_k=40, top_p=None, max_new_tokens=min(120, cfg.block_size // 2)))
    print(bytes(token for token in tokens if token < 256).decode('utf-8', errors='replace'))


if __name__ == '__main__':
    main()
