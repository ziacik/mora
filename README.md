# Mora 0.4.1

Mora is an experimental affective programming language. Applications describe beliefs, evidence, desires, journeys, laws, offers, scenes, and calls to generic faculties.

The runtime must not know an application's domain. In Mora 0.4, desires are executed from the AST through generic operations such as `ask`, `attempt`, `remember`, `snapshot`, `for every`, and `perceive`. A faculty provider exposes general capabilities; application concepts, prompts, thresholds, names, and workflows stay in the `.mora` program.

## Install on Arch / Manjaro

```bash
sudo pacman -S python python-gobject python-cairo gtk4 libadwaita python-pillow python-opencv python-requests sane libsecret
sh install.sh
```

## Commands

```bash
mora check app.mora
mora inspect app.mora
mora simulate app.mora 'correction(A)' 'correction(A)' 'correction(A)'
mora run app.mora
```

## Generic execution model

A desire is not dispatched by name inside the runtime:

```mora
desire LoadSomething {
    ask files.choose-image as source
    attempt images.load source as value
    remember document as value
    perceive loaded
}
```

The runtime only interprets the generic verbs and resolves declared faculties. The same executor can run another application with completely different desire names and domain concepts.

Vision is also generic. A program supplies its own concept and desired result shape; the OpenAI Responses faculty turns that concept into a structured request. The runtime contains no domain prompt for any particular application.

## Faculties

Current provider adapters include:

- GTK4/libadwaita scenes, gestures, keyboard input, periodic clocks, and 2D canvas primitives
- generic arithmetic and 2D motion/collision operations
- desktop file/form interaction
- image codecs
- SANE acquisition
- OpenCV quadrilateral geometry
- OpenAI Responses vision
- system keyring secrets

These are language/platform capabilities, not application implementations.

## Anti-cheat rule

CI scans the runtime source for identifiers and prompt fragments belonging to the reference application. If those leak into `mora/*.py`, the build fails. CI also checks and starts the reference Mora application headlessly, so removing domain knowledge may not break execution.

Reference application compatibility is checked against its current `main` branch in CI.

## Interactive scenes

GTK scenes may bind key press/release events to desires and may invite a desire periodically:

```mora
keyboard {
    key w pressed invites MoveUp
    key w released invites StopMoving
}

clock {
    every 16ms invites AdvanceWorld
}
```

A canvas may also declare generic `rectangle`, `circle`, `line`, and `text` primitives bound to remembered values. These mechanisms contain no application rules; applications define their own state transitions in Mora desires.
