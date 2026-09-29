# Mora 0.3

Mora is an experimental **affective programming language**. Programs express beliefs, evidence, desires, journeys, laws, offers and declarative scenes instead of wiring ordinary callbacks throughout application code.

This repository contains the Mora 0.3 runtime. Version 0.3.1 includes the first GTK4/libadwaita execution backend.

## Install on Arch / Manjaro

The language runtime is Python-based; GTK and native faculties use system libraries.

```bash
sudo pacman -S python python-gobject gtk4 libadwaita python-pillow python-opencv python-requests sane libsecret
./install.sh
```

`install.sh` creates an isolated runtime venv with access to system site packages and installs `mora` into `~/.local/bin`.

## Commands

```bash
mora check app.mora
mora inspect app.mora
mora simulate app.mora 'correction(A)' 'correction(A)' 'correction(A)'
mora run app.mora
```

## GTK execution

`mora run` reads the Mora AST directly:

- `scene` / `window` / `header` / `sidebar` / `canvas` become GTK4/libadwaita UI;
- `button ... invites Desire` dispatches a Mora desire;
- declared gestures become GTK event controllers;
- `faculty` declarations are fulfilled by native adapters (GTK, SANE, OpenCV, image codecs, OpenAI Responses, system keyring);
- perceptions feed the affective engine, which updates beliefs and evaluates journeys;
- journey `offer` / `withdraw` changes UI assistance such as magnifier, handle size and snapping.

The application itself does **not** contain Python/Rust callbacks. Python here is the language runtime, analogous to CPython being the runtime for Python source.

## Example

```mora
belief user about emotion {
    confident   0.45
    uncertain   0.20
    frustrated  0.05
    never certain
}

meaning repeated correction(frame) within 12s {
    suggests frustrated strongly
    suggests uncertain moderately
}

journey Editing {
    toward user.confident
    away from user.frustrated

    when user struggles with current frame {
        offer PreciseEditing
    }
}
```

## Status

Implemented:

- recursive `bring`
- Mora 0.3 block parser
- rejection of host-language-shaped application constructs (`fn`, `state.foo`, lambdas, callback wiring)
- beliefs and evidence-producing `meaning`
- temporal repeated-event evidence
- patterns and journeys
- offers / withdrawals
- `check`, `inspect`, `simulate`
- GTK4/libadwaita scene backend
- image loading/export
- SANE scanning
- OpenCV quadrilateral/perspective operations
- OpenAI Responses vision faculty
- system keyring faculty

Mora is experimental; the grammar is intentionally small and is evolving alongside real applications such as Scan Slicer Emo.
