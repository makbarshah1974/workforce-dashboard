# Render (and generic gunicorn hosts) start command.
# init_db() creates the schema + seeds default users/data before serving,
# then Gunicorn serves the Flask app. Binds to the $PORT Render provides.
web: python -c "from wsgi import init_db; init_db()" && gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120