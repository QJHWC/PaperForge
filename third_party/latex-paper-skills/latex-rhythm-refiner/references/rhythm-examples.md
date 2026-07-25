# Rhythm Refinement Examples

Before/after pairs showing how to apply the rhythm principles from the
SKILL.md.  Each example targets one or two principles; real passes combine
all of them.

---

## 1. Varying Sentence Length

### Before (monotonous — all medium)

```latex
Deep learning models have been widely applied to image classification tasks.
These models typically rely on large-scale labeled datasets for training.
Transfer learning has emerged as a practical alternative for this problem.
Pre-trained models are fine-tuned on domain-specific data to improve results.
This approach reduces the need for extensive annotation efforts significantly.
```

### After (mixed short / medium / long)

```latex
Deep learning dominates image classification.
These models depend on large labeled datasets---a bottleneck when annotation
budgets are tight~\cite{deng2009imagenet}.
Transfer learning sidesteps this constraint: a model pre-trained on a broad
corpus is fine-tuned on domain-specific samples, cutting annotation costs
while preserving accuracy~\cite{yosinski2014transferable}.
The savings compound as domains narrow.
```

**What changed:** Five same-length sentences became four sentences of varying
length (short / long / long / short).  A two-sentence paragraph could follow
for emphasis.

---

## 2. Removing Filler Phrases

### Before

```latex
In order to evaluate the effectiveness of our proposed method, we conducted
a large number of experiments on several benchmark datasets.  It is worth
noting that our method achieves state-of-the-art results due to the fact
that it leverages multi-scale feature representations.  At the present time,
no existing approach combines these two strategies for the purpose of
improving robustness.
```

### After

```latex
We evaluated our method on several benchmark datasets.
It achieves state-of-the-art results because it leverages multi-scale
feature representations~\cite{lin2017fpn}.
No existing approach combines these two strategies to improve robustness.
```

**Replacements applied:**
| Filler | Replacement |
|---|---|
| In order to | (restructured) |
| a large number of | several |
| It is worth noting that | (deleted) |
| due to the fact that | because |
| At the present time | (deleted) |
| for the purpose of | to |

---

## 3. Eliminating Unnecessary Transitions

### Before

```latex
However, existing methods suffer from high computational cost.
Furthermore, they require task-specific architectures.
Moreover, the lack of interpretability limits clinical adoption.
Therefore, we propose a lightweight, interpretable framework.
```

### After

```latex
Existing methods suffer from high computational cost and require
task-specific architectures~\cite{chen2021cost}.
The lack of interpretability further limits clinical adoption.
We propose a lightweight, interpretable framework that addresses
all three limitations.
```

**What changed:** Four transition-heavy sentences collapsed into three.
Structure alone conveys the contrast (problems then solution), so
"However/Furthermore/Moreover/Therefore" are unnecessary.

---

## 4. Tightening Prose (Active Voice + Concrete Verbs)

### Before

```latex
It was shown by previous work that attention mechanisms are effective
for the task of sequence modeling.  The model that was proposed by
Vaswani et al. does sequence transduction using self-attention.
This approach works well for capturing long-range dependencies.
```

### After

```latex
Attention mechanisms excel at sequence
modeling~\cite{bahdanau2015attention}.
Vaswani et al.\ introduced the Transformer, which replaces recurrence
with self-attention to capture long-range dependencies in a single
pass~\cite{vaswani2017attention}.
```

**What changed:**
- Passive ("It was shown by") replaced with active voice.
- Vague verbs ("does", "works well") replaced with concrete verbs
  ("excel", "introduced", "replaces", "capture").
- Three sentences compressed to two without information loss.

---

## 5. Paragraph Length Variation

### Before (wall of text — 8 uniform sentences)

```latex
Reinforcement learning has been applied to robotic manipulation.
Early approaches used hand-crafted reward functions.
These reward functions were difficult to design.  Inverse reinforcement
learning was proposed to address this limitation.  However, inverse
reinforcement learning requires expert demonstrations.  Collecting
expert demonstrations is expensive.  Recent methods use self-supervised
objectives instead.  Self-supervised methods reduce the dependence on
human supervision.
```

### After (short + medium + short paragraphs)

```latex
Reinforcement learning has been applied to robotic
manipulation~\cite{levine2016end}, but early approaches relied on
hand-crafted reward functions that were difficult to design.

Inverse reinforcement learning~\cite{abbeel2004apprenticeship}
addresses this by inferring rewards from expert demonstrations.
Collecting such demonstrations remains expensive, particularly for
contact-rich tasks where even skilled operators struggle to provide
consistent trajectories.

Recent self-supervised objectives reduce dependence on human
supervision entirely~\cite{pinto2016supersizing}.
```

**What changed:** One 8-sentence block became three paragraphs
(2 sentences / 3 sentences / 1 sentence), each with a clear focus.

---

## 6. Combined Pass (All Principles)

### Before

```latex
In this section, we present the results of our experiments.
As mentioned above, we evaluated our method on three datasets.
It is important to note that our approach outperforms all baselines.
Table 1 shows the comparison results.  Moreover, our method achieves
a significant improvement in terms of the F1 score.  Furthermore,
the training time of our method is comparable to that of the baseline
methods.  In order to further validate our findings, we conducted an
ablation study.  The results of the ablation study are shown in Table 2.
```

### After

```latex
We evaluated our method on three datasets (Table~\ref{tab:main}).
It outperforms all baselines, with the largest gains on
F1---3.2 points above the nearest competitor~\cite{wang2023baseline}.
Training time remains comparable to existing methods.

An ablation study (Table~\ref{tab:ablation}) isolates the contribution
of each component.
```

**What changed:**
- Filler removed: "In this section, we present", "As mentioned above",
  "It is important to note", "In order to", "in terms of".
- Transitions removed: "Moreover", "Furthermore".
- 8 sentences became 4 across two paragraphs.
- Vague "significant improvement" replaced with concrete figure.
- Active voice throughout.
