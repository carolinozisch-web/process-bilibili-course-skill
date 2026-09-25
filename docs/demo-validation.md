# Demo validation

Validation date: 2026-09-25

## New-link candidates

Three public videos on the same topic were selected for the final fresh-import demo:

| Video | Approximate duration | Local state |
|---|---:|---|
| [No.016 费曼学习法](https://www.bilibili.com/video/BV1GF411c7pJ) | 3:22 | Imported, awaiting transcription |
| [费曼学习法：提高学习效率和记忆力](https://www.bilibili.com/video/BV11d4y1H7NA) | 2:07 | Imported, awaiting transcription |
| [5 分钟学会费曼技巧](https://www.bilibili.com/video/BV1UE411y7mw) | about 5 minutes | Imported, awaiting transcription |

The public pages were discoverable, but Bilibili's metadata API returned HTTP 412 from the validation host. The product keeps these records in `discovered` state and exposes a later `开始处理` action. No transcript or AI result was fabricated.

## Completed local loop

The full product loop was exercised in an isolated demo workspace with three real, already archived episodes from one regression-analysis course:

- Lasso and Ridge regularization
- PCR and PLS dimensionality reduction
- Binary logistic regression

The run produced three reviewed sources, five source-linked method cards, and passed these five retrieval checks:

1. How to use Lasso for feature selection
2. The difference between Ridge and Lasso
3. How PCR selects the number of components
4. When to use PLS
5. How to model a binary outcome with logistic regression

Every top result linked to an original public source and a timestamp. This validates the review, curation, retrieval, and traceability loop; the three fresh links above remain the pending network-dependent acceptance step.
