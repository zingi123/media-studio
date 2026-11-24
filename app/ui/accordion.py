"""Accordion behaviour for the left workflow panel (no Qt imports: pure logic, unit-testable).

An item is anything that exposes ``.btn`` - a checkable button with ``isChecked()``, ``setChecked(bool)`` and a
``toggled`` signal (``SectionBox`` / ``Collapsible`` do). Opening one item closes every other one, so at most one
item is expanded at a time. Closing the open item (header click or the rail button again) is allowed: then none is open.

Nothing is created or duplicated here: the group only listens to the existing header buttons.
"""


class AccordionGroup:
    def __init__(self, items):
        self.items = list(items)
        self._busy = False
        opened = [it for it in self.items if it.btn.isChecked()]
        for it in opened[1:]:           # enforce the invariant at start-up: keep only the first open item
            it.btn.setChecked(False)
        for it in self.items:
            it.btn.toggled.connect(lambda on, _it=it: self._toggled(_it, on))

    def _toggled(self, item, on):
        if not on or self._busy:
            return
        self._busy = True
        try:
            for other in self.items:
                if other is not item and other.btn.isChecked():
                    other.btn.setChecked(False)
        finally:
            self._busy = False

    def open_item(self):
        """The item that is currently expanded, or None."""
        for it in self.items:
            if it.btn.isChecked():
                return it
        return None

    def toggle(self, item):
        """Rail-button behaviour: open ``item`` (closing the others) or, if it is already open, collapse it.
        Returns True when the item ends up open."""
        if item.btn.isChecked():
            item.btn.setChecked(False)
            return False
        item.btn.setChecked(True)
        return True
