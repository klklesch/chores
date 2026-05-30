import os

from flask import Blueprint, current_app, send_from_directory, session, redirect, url_for

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def home():
    return redirect(url_for('child.select'))


@main_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('child.select'))


@main_bp.route('/avatars/<path:filename>')
def avatar_file(filename):
    return send_from_directory(os.path.join(current_app.instance_path, 'avatars'), filename)
