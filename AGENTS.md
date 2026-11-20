# ring2

ring2 is a small tool that runs a fleet of coding agents. You are changing its source.

- Make the smallest change that does the task. Add no feature, setting, record type, state or approval step the task does not ask for.
- The core never names a harness. Code for one harness goes in `ring2/providers/`; a test enforces this.
- Import only the Python standard library.
- Run the tests with `python3 -B -m unittest discover -s tests -t . -p 'test_*.py'`. The suite takes several minutes and makes no provider request; let it finish.
- Before changing how the files fit together, read "How ring2 is put together" in CONTRIBUTING.md.
- A question or a blocker that no role can settle goes to @controller on the channel.
