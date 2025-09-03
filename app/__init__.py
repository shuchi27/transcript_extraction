from flask import Flask

def create_app():
    app = Flask(__name__)          # templates live in app/templates
    from .routes import api_bp
    app.register_blueprint(api_bp) # ← NO url_prefix
    print("URL MAP:", app.url_map) # debug
    return app
