"""Conservative generated-source guard; not a sandbox for arbitrary TeX.

Original document macros/packages remain trusted inputs. Compilation must still
run without shell escape in an isolated, least-privilege working directory.
"""

import re

FORBIDDEN_LATEX = re.compile(
    r"\^\^"
    r"|\\[A-Za-z@]*@|\\[A-Za-z]+_[A-Za-z_]+:[A-Za-z]*"
    r"|\\(?:input|include|includeonly|InputIfFileExists|IfFileExists|"
    r"includegraphics|includepdf|verbatiminput|lstinputlisting|inputminted|"
    r"CatchFileDef|CatchFileEdef|openin|openout|read|readline|write|write18|"
    r"closein|closeout|immediate|ShellEscape|directlua|latelua|luaexec|special|"
    r"csname|endcsname|catcode|mathcode|lccode|uccode|scantokens|"
    r"def|edef|gdef|xdef|let|futurelet|expandafter|expanded|unexpanded|"
    r"newcommand|renewcommand|providecommand|DeclareRobustCommand|"
    r"newenvironment|renewenvironment|ExplSyntaxOn|makeatletter|"
    r"pdfobj|pdfxform|pdfximage|pdffiledump|pdfextension)(?![A-Za-z@])"
    r"|\\begin\s*\{\s*filecontents\*?\s*\}"
    r"|\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\]\s*)?"
    r"\{[^{}]*\bshellesc\b[^{}]*\}",
    re.IGNORECASE,
)
