import os
from dataclasses import dataclass, field
from pathlib import Path

from .visualize import visualize_sweep
from ..files import elk_reporter_dir

from simple_parsing import field

@dataclass
class Plot:
    sweeps: list[str] = field(positional=True, default_factory=list)
    def execute(self):
        sweeps_root_dir = Path.home() / elk_reporter_dir() / "sweeps"

        sweep = max(sweeps_root_dir.iterdir(), key=os.path.getctime)
        if self.sweeps:
            sweep = sweeps_root_dir / self.sweeps[0]
            if not sweep.exists():
                print(f"No sweep with name {self.sweeps[0]} found in {sweeps_root_dir}")
                return
        if len(self.sweeps) > 1:
            # TODO support more than one sweep
            print(
                f"""{len(self.sweeps)} paths specified.
                Only one sweep is supported at this time."""
            )
        else:
            visualize_sweep(sweep)