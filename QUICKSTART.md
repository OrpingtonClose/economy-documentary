# How to Make a Movie

You need two things: a topic and an LLM API key.

## Step 0: Set up your API key

The pipeline uses an AI model to write the documentary script. You need
one of these (not both):

```bash
# Option A: Google Gemini (free tier available)
export GOOGLE_API_KEY=your-key-here
# Get one at: https://aistudio.google.com/apikey

# Option B: OpenAI
export OPENAI_API_KEY=your-key-here
# Get one at: https://platform.openai.com/api-keys
```

If you already have one of these set in your environment, you're good.
The script will detect it automatically.

## Step 1: Run this command

```bash
./make_movie.sh "The History of Coffee"
```

Replace "The History of Coffee" with whatever you want your documentary to be about.

That's it. The script handles everything else — installing dependencies,
setting up configuration, and running the pipeline.

## What happens next

The script will:

1. Check that Python 3.12+ is installed
2. Install Poetry (if not already installed)
3. Install all dependencies
4. Create a configuration file with safe defaults
5. Generate a research corpus from your topic
6. Run the full pipeline in test mode (no GPU needed)
7. Tell you where your output files are

The whole process takes 2-5 minutes.

## If you have your own research

If you've already written research notes, articles, or transcripts about
your topic, you can feed them in:

```bash
./make_movie.sh "The History of Coffee" --corpus my_research.md
```

The corpus file should be a markdown file with your research material.
There's an example at `examples/sample_corpus.md`.

## If something goes wrong

The script will tell you exactly what's wrong and what to do about it.
The most common issues are:

- **"Python 3 is not installed"** → Install Python 3.12 from https://python.org
- **"Poetry not found"** → The script will try to install it automatically
- **"Dependencies failed"** → Run `cd server && poetry install` and look at the error

## Quick test (faster)

If you want a very fast test run (2 scenes, about 1 minute of content):

```bash
./make_movie.sh "The History of Coffee" --quick
```

## Production mode (real video)

When you're ready to generate real video with GPU workers:

```bash
./make_movie.sh "The History of Coffee" --corpus my_research.md --production
```

This requires:
- A Vast.ai account with API key (set `VAST_API_KEY` in `server/.env`)
- An LLM API key (set `OPENAI_API_KEY` or `GOOGLE_API_KEY` in `server/.env`)
- About $5-15 in GPU costs per documentary
