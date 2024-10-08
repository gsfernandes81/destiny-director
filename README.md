# Destiny Director

Setting up the dev environment:

1. Get a working [Poetry](https://python-poetry.org/) installation
2. Run `poetry install` in the root of the git clone
3. `poetry shell` to jump into the virtualenv
4. [Optional] Set up the `.env` with environment variables refering to `anchor/cfg.py` & `.env-example`

Running code locally:

```
make run-local
```

Running tests locally:

```
make test
```

Running code locally with docker:

```
docker build -t anchor .
docker run --env-file=.env anchor
```

Deploying code to [railway](https://railway.app/)

Make sure you have the [railway cli](https://docs.railway.app/develop/cli) installed and are logged in
Use this to deploy to the dev instance on railway
```
make deploy-dev
```

**CAUTION** use this to deploy to the production instance on railway
```
make deploy-prod
```