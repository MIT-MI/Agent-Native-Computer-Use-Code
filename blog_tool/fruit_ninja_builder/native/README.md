# Evaluator side (Windows Fruit Ninja HD)

Code in this directory runs only for the evaluator. The Builder and deployed MCPs never see it: solve
desktops mount the repository under `/opt/fn-eval`, which non-root users cannot enter.

| File                                                     | Role                                                                                                                                    |
| -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `eval-session.mjs`                                       | One round as root: reads game memory from `/proc/<pid>/mem`, enters Arcade on the player's first tool call, records the game window     |
| `cli.mjs`                                                | `GameController` (native state, Arcade start through the visible menu, GUI strokes) and the screenshot GUI tools                        |
| `state.mjs`, `re-layout.json`                            | Decoding of entities, score, clock and phase from game memory using data-driven offsets                                                 |
| `RE-NOTES.md`                                            | How that memory layout was recovered and checked                                                                                        |
| `desktop.mjs`, `desktop.py`                              | Persistent XTest input and screenshot worker                                                                                            |
| `recording.mjs`, `viewer.mjs`                            | Game-only video (`video.mp4`) and the evaluator's JSONL event log (`viewer-events.jsonl`)                                               |
| `setup.py`, `build-dxvk.sh`, `dxvk-2.4-fruit-ninja.patch` | Download and verify the pinned game (parameters in `config/game.json`); build DXVK 2.4 with the Fruit Ninja singular-normal-matrix fix |

`make setup` runs `setup.py` in the runtime image, initializes a Wine prefix and freezes the game and
prefix as `.local/windows/template`. Every desktop copies that template, so each round starts from the
same bytes and saved progress never carries over.

Containers have no sound card, so the image routes ALSA to a null device; the game exits otherwise.
