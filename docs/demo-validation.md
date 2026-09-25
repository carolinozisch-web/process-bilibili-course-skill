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

## Expanded knowledge-base validation

The local knowledge base was then expanded with all 12 episodes of the regression-analysis course. The set covers data preparation, simple and multiple regression, diagnostics, nonlinear terms, multicollinearity, influential observations, subset selection, regularization, dimensionality reduction, and logistic regression.

- 12 real video sources reviewed and curated
- 21 source-linked method cards
- 10 natural-language retrieval questions
- 10/10 checks returned the expected card within the top five results
- Every evaluated card retained its original video URL and segment timestamp

The first run passed 6/10 questions. The failures exposed weak Chinese token splitting in SQLite's default full-text behavior. The basic retriever was updated to add bounded adjacent Chinese terms and deduplicate multi-hit results; the same questions then passed 10/10. This remains an explainable full-text heuristic, not semantic or embedding retrieval.

## Answer-first search verification

The search interface was rechecked with the question “怎样判断线性回归模型是否可靠”. In basic mode it now returns the extracted diagnostic procedure directly, separates reviewed knowledge from raw-source matches, and keeps raw videos collapsed by default. The first evidence link resolves to Bilibili part 4 at 30 seconds with `?p=4&t=30`; desktop and narrow-window checks showed no horizontal overflow.
