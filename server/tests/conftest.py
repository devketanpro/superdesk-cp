import flask
import pytest


@pytest.fixture(autouse=True)
def app():
    app = flask.Flask(__name__)
    app.config.update(
        {
            "VERSION": "version",
            "DEFAULT_LANGUAGE": "en",
            "DEFAULT_AUTHOR_EN": "cpdefaultauthor",
        }
    )
    ctx = app.app_context()
    ctx.push()
    yield app
    ctx.pop()
