venv:
	uv sync

format: venv
	uv run ruff format
	uv run ruff check --fix

run: venv
	@uv run python src/fitbot_cli.py --email='$(email)' --booking-goals='$(booking-goals)' --box-name='$(box-name)' --box-id='$(box-id)' --days-in-advance='$(days-in-advance)' --proxy='$(proxy)'

tests: venv
	uv run pytest src/tests

docker/build:
	docker build --no-cache	--tag=fitbot .

docker/tests:
	 docker run fitbot /bin/sh -c 'make tests'
