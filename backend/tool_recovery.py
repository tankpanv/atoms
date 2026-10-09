"""Bound repeated tool failures without terminating otherwise recoverable jobs."""
class ToolRecovery:
    def __init__(self):
        self.failures = {}
        self.cooldown = {}

    def available(self, tools):
        return [tool for tool in tools if not self.cooldown.get(tool['function']['name'], 0)]

    def next_turn(self):
        self.cooldown = {name: turns - 1 for name, turns in self.cooldown.items() if turns > 1}

    def record(self, name, failed):
        if self.cooldown.get(name):
            return False
        self.failures[name] = self.failures.get(name, 0) + 1 if failed else 0
        if self.failures[name] < 3:
            return False
        self.failures[name] = 0
        self.cooldown[name] = 3
        return True
