"""Run the retry queue (data/failed_downloads.json) in the background."""

from PySide6.QtCore import QThread, Signal

from managers import retry_manager


class RetryWorker(QThread):
    log = Signal(str)
    finished_summary = Signal(dict)

    def run(self):
        if not retry_manager.is_configured():
            self.log.emit("[retry] The retry manager isn't set up (start the GUI with gui_main.py).")
            self.finished_summary.emit(dict(recovered=0, still_failing=0, not_attempted=0,
                                            stopped_reason="not configured"))
            return
        self.log.emit("[retry] Retrying the failed downloads...")
        try:
            summary = retry_manager.run()             # no confirm: the button was the confirm
        except RuntimeError as error:                 # a run is already going
            self.log.emit(f"[retry] {error}")
            self.finished_summary.emit(dict(recovered=0, still_failing=0, not_attempted=0,
                                            stopped_reason=str(error)))
            return
        except Exception as error:
            self.log.emit(f"[retry] failed: {type(error).__name__}: {error}")
            self.finished_summary.emit(dict(recovered=0, still_failing=0, not_attempted=0,
                                            stopped_reason="error"))
            return
        result = dict(recovered=len(summary.recovered),
                      still_failing=len(summary.still_failing),
                      not_attempted=len(summary.not_attempted),
                      stopped_reason=summary.stopped_reason or "")
        self.log.emit(f"[retry] {result['recovered']} recovered, "
                      f"{result['still_failing']} still failing"
                      + (f", stopped ({result['stopped_reason']})" if result["stopped_reason"] else ""))
        self.finished_summary.emit(result)
