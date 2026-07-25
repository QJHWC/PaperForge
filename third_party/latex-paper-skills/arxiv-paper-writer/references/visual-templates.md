# Visual Guidance for Review / Survey Papers

## Content-to-Visualization Triggers

| Content Pattern | Visualization Type | Sizing |
|----------------|-------------------|--------|
| 3+ related concepts | Concept map | `figure` / `\columnwidth` |
| Process or pipeline | Flowchart | `figure` / `\columnwidth` |
| System architecture | Architecture diagram | `figure*` / `\textwidth` |
| Performance comparison | Table (+ optional plot) | `table` / `\columnwidth` |
| Historical development | Timeline | `figure*` / `\textwidth` |
| Decision logic | Decision tree | `figure` / `\columnwidth` |
| Relationships | Node/link diagram | `figure` / `\columnwidth` |
| Taxonomies/categories | Classification tree | `figure` or `figure*` |
| 4+ methods across 3+ dimensions | Comparison matrix | `table*` / `\textwidth` |
| Coverage of sub-areas vs years | Literature heatmap | `figure` / `\columnwidth` |
| Evolution of a research thread | Annotated timeline | `figure*` / `\textwidth` |

## General Requirements

- **Two-column aware sizing**: use `figure` + `\columnwidth` by default; switch
  to `figure*` + `\textwidth` only when labels/columns require it.
- If a figure includes externally sourced content (nodes, labels, data), add
  citations — usually in the caption to avoid clutter.
- Minimum **5 distinct visualization types** per paper.
- Every figure/table must be referenced in text.
- Keep designs simple and readable; prefer low-saturation color accents.
- Adapt visuals to content; do not copy placeholders verbatim.

---

## Pattern 1: Taxonomy / Classification Tree

Use when the paper organizes methods into a hierarchy (common in survey papers).

```latex
\begin{figure}[t]
\centering
\begin{tikzpicture}[
    level 1/.style={sibling distance=38mm, level distance=14mm},
    level 2/.style={sibling distance=20mm, level distance=12mm},
    every node/.style={draw, rounded corners, font=\small,
        minimum height=6mm, inner sep=2pt, align=center},
    root/.style={fill=blue!12},
    cat/.style={fill=gray!10},
    leaf/.style={fill=white, font=\scriptsize},
]
\node[root] {Research Area}
  child { node[cat] {Category A}
    child { node[leaf] {Method 1\\{\tiny\cite{ref1}}} }
    child { node[leaf] {Method 2\\{\tiny\cite{ref2}}} }
  }
  child { node[cat] {Category B}
    child { node[leaf] {Method 3\\{\tiny\cite{ref3}}} }
    child { node[leaf] {Method 4\\{\tiny\cite{ref4}}} }
  }
  child { node[cat] {Category C}
    child { node[leaf] {Method 5\\{\tiny\cite{ref5}}} }
  };
\end{tikzpicture}
\caption{Taxonomy of approaches in [area]. Categories derived from
  [organizing principle].}
\label{fig:taxonomy}
\end{figure}
```

**When to use**: the paper has a Related Work section that groups methods into
families, or the paper's core contribution IS a taxonomy.

---

## Pattern 2: Annotated Timeline

Use to show the evolution of a research area or method family.

```latex
\begin{figure*}[t]
\centering
\begin{tikzpicture}
  \draw[->, thick] (0,0) -- (14,0) node[right] {\small Year};
  % Year ticks
  \foreach \x/\y in {1/2018, 3.5/2019, 6/2020, 8.5/2022, 11/2024} {
    \draw (\x, -0.15) -- (\x, 0.15);
    \node[below, font=\scriptsize] at (\x, -0.2) {\y};
  }
  % Milestone nodes (alternate above/below)
  \node[above, draw, rounded corners, fill=blue!8, font=\scriptsize,
    text width=20mm, align=center] at (1, 0.5) {Seminal Work~\cite{ref1}};
  \node[below, draw, rounded corners, fill=orange!8, font=\scriptsize,
    text width=20mm, align=center] at (3.5, -0.7) {Key Advance~\cite{ref2}};
  \node[above, draw, rounded corners, fill=blue!8, font=\scriptsize,
    text width=20mm, align=center] at (6, 0.5) {Paradigm Shift~\cite{ref3}};
  \node[below, draw, rounded corners, fill=orange!8, font=\scriptsize,
    text width=20mm, align=center] at (8.5, -0.7) {Scaling Era~\cite{ref4}};
  \node[above, draw, rounded corners, fill=green!10, font=\scriptsize,
    text width=20mm, align=center] at (11, 0.5) {Current SOTA~\cite{ref5}};
\end{tikzpicture}
\caption{Timeline of key developments in [area]. Blue nodes denote
  foundational work; orange nodes mark paradigm transitions.}
\label{fig:timeline}
\end{figure*}
```

**When to use**: the Introduction or Background section traces a historical
progression through 4+ milestone works.

---

## Pattern 3: Method Comparison Matrix

A structured table comparing methods across multiple dimensions.

```latex
\begin{table*}[t]
\centering
\caption{Comparison of representative methods. \checkmark = supported;
  \texttimes = not supported; $\sim$ = partial.}
\label{tab:comparison}
\small
\begin{tabular}{lcccccc}
\toprule
\textbf{Method} & \textbf{Dim 1} & \textbf{Dim 2} & \textbf{Dim 3}
  & \textbf{Dim 4} & \textbf{Dataset} & \textbf{Year} \\
\midrule
Method A~\cite{ref1} & \checkmark & \texttimes & $\sim$ & \checkmark & D1 & 2020 \\
Method B~\cite{ref2} & \checkmark & \checkmark & \texttimes & \texttimes & D1,D2 & 2022 \\
Method C~\cite{ref3} & \texttimes & \checkmark & \checkmark & \checkmark & D2 & 2023 \\
\bottomrule
\end{tabular}
\end{table*}
```

**When to use**: Related Work or Analysis section compares 4+ methods across
3+ qualitative or quantitative dimensions.

---

## Pattern 4: Literature Coverage Heatmap

Shows which sub-areas are covered by which groups of references.

```latex
\begin{figure}[t]
\centering
\begin{tikzpicture}[
    cell/.style={minimum width=10mm, minimum height=7mm, font=\scriptsize,
        inner sep=0pt},
    header/.style={cell, font=\scriptsize\bfseries, rotate=45, anchor=south west},
]
  % Row labels (sub-areas)
  \foreach \i/\lbl in {0/Sub-area A, 1/Sub-area B, 2/Sub-area C, 3/Sub-area D} {
    \node[anchor=east, font=\scriptsize] at (-0.2, -\i*0.8) {\lbl};
  }
  % Column headers (year ranges)
  \foreach \j/\yr in {0/Pre-2020, 1/2020--22, 2/2023--24, 3/2025+} {
    \node[header] at (\j*1.1+0.55, 0.7) {\yr};
  }
  % Heatmap cells: fill opacity encodes paper count
  % (adapt fill values to actual citation counts)
  \foreach \i/\j/\cnt/\op in {
    0/0/3/0.3, 0/1/8/0.6, 0/2/12/0.9, 0/3/4/0.35,
    1/0/1/0.1, 1/1/5/0.45, 1/2/7/0.55, 1/3/2/0.2,
    2/0/6/0.5, 2/1/4/0.35, 2/2/9/0.7, 2/3/5/0.45,
    3/0/0/0.0, 3/1/2/0.2, 3/2/3/0.3, 3/3/1/0.1} {
    \node[cell, fill=blue!\op*100] at (\j*1.1+0.55, -\i*0.8) {\cnt};
  }
\end{tikzpicture}
\caption{Literature coverage by sub-area and publication period.
  Darker cells indicate more cited papers in that cluster.}
\label{fig:coverage}
\end{figure}
```

**When to use**: the paper explicitly discusses literature coverage gaps, or
the Related Work section organizes papers by both topic and era.

---

## Pattern 5: Concept Flow / Pipeline Diagram

For illustrating how components in a surveyed system connect.

```latex
\begin{figure}[t]
\centering
\begin{tikzpicture}[
    box/.style={draw, rounded corners, minimum height=8mm,
        minimum width=22mm, font=\small, align=center},
    arr/.style={->, thick, >=stealth},
]
  \node[box, fill=blue!10]  (a) at (0,0)   {Input};
  \node[box, fill=gray!10]  (b) at (3.2,0) {Stage 1};
  \node[box, fill=gray!10]  (c) at (6.4,0) {Stage 2};
  \node[box, fill=green!10] (d) at (9.6,0) {Output};
  \draw[arr] (a) -- (b);
  \draw[arr] (b) -- (c);
  \draw[arr] (c) -- (d);
\end{tikzpicture}
\caption{General pipeline shared by most [area] approaches.}
\label{fig:pipeline}
\end{figure}
```

---

## Style Rules

- **Color palette**: novel/highlighted = `blue!10`, standard = `gray!10`,
  data flow = `orange!10`, external = `green!10`. Keep fills low-saturation.
- **Line widths**: 0.4pt for box borders, `thick` (0.8pt) for arrows.
- **Font sizing**: `\small` for node text, `\scriptsize` for annotations.
- **Caption style**: evidence-first; include citations when sourced content
  is adapted.

## Notes

- Use the approved outline to choose visuals; avoid inventing sections to fit
  a diagram.
- Taxonomy trees and comparison matrices are the highest-priority visualization
  types for review papers — every survey should have at least one of each.
- Prefer TikZ for all diagrams (native LaTeX, best quality, no external files).
