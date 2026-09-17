# Model notes

## Qwen3.6 35B-A3B

Mixture-of-experts model with about 3 billion active parameters per token. It is the default for interactive use. The Q4_K_M quantization from Unsloth (UD) keeps quality close to Q8 on the eval set while fitting in 20 GB of shared memory together with a 32K context.

## Qwen3.6 27B dense

Dense model, better on multi-step math but about seven times slower to generate. Only use it for offline batch jobs through the `deep` profile.

## Quantization comparison

| Quant | File size | Eval score | Generation speed |
|---|---|---|---|
| Q4_K_M | 17.3 GB | 0.86 | 5.9 tok/s |
| Q5_K_M | 20.1 GB | 0.87 | 5.1 tok/s |
| Q8_0 | 30.2 GB | 0.88 | does not fit |

Q5_K_M was rejected because the extra 2.8 GB leaves too little room for the KV cache at 32K context.

## Draft models

A 0.6B draft model for speculative decoding was tested and removed. Acceptance rate stayed below 40 percent on chat prompts, so it made generation slower overall.
