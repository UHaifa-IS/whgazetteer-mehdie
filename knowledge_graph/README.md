# knowledge_graph

Early pyRML experiments for exporting the gazetteer database to RDF. The
maintained knowledge-graph code lives in the `mehdie-knowledge-graph` repository.

## Database credentials

Scripts that connect to the MEHDIE Postgres database read their connection
settings from environment variables, never from code:

| Variable | Default | Notes |
| --- | --- | --- |
| `DB_HOST` | `localhost` | Use an SSH tunnel to the server, e.g. `ssh -L 5432:localhost:5432 cs-mehdie-web01.srv.aau.dk` |
| `DB_PORT` | `5432` | |
| `DB_NAME` | - | `mehdie-gui` |
| `DB_USER` | - | |
| `DB_PASSWORD` | - | Ask the project lead; never commit it |

Copy the `.env.example` next to the script to `.env` and fill it in, or export
the variables in your shell. `.env` files are git-ignored. The scripts load
`.env` automatically when `python-dotenv` is installed (`pip install python-dotenv`).
