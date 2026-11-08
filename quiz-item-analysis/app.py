from vumlo_plugins.http import create_plugin_app
from vumlo_plugins.protocol import manifest
from vumlo_plugins.protocol import Notifier
MANIFEST = manifest("quiz-item-analysis", "1.0.0", subscribes=("quiz.attempt_submitted",), scopes=("quizzes:read", "reports:write"))
notifier = Notifier()
def process(kind, event):
    if kind == "quiz.attempt_submitted":
        notifier.send("Quiz item analysis updated", "A quiz attempt was submitted.", event)
app = create_plugin_app(MANIFEST, process)
