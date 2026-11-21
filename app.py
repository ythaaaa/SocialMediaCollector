from src.utils.diagnostics import initialize_diagnostics

# Log startup before importing the UI, browser, or download stack so early
# import and Qt initialization failures have a stable diagnostic destination.
initialize_diagnostics()

from src.ui.main_window import run

if __name__ == "__main__":
    run()
