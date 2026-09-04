# update-voyage-output

Tooling to regenerate the **documented output** of the Voyage AI docs
(`content/voyageai` in `docs-mongodb-internal`) after a major Voyage AI model
release, routing LLM API calls through MongoDB's **Grove** gateway.

### Clone this utility down to your local machine: 

```git clone git@github.com:jvincent-mongodb/update-voyage-output.git```

## Procedure

```bash
#1. Activate the .venv
source .venv/bin/activate

# 2. set required env vars
export GROVE_API_KEY=...       # required
export VOYAGE_API_KEY=...      # required
export MONGODB_URI=...         # required

# 3. Run the utility
python update_voyage_output.py all --assets-dir ~/update-voyage-output/multimodal-images --docs-repo </path/to/your/monorepo/clone>
```

## Help

```
.venv/bin/python update_voyage_output.py -h
.venv/bin/python update_voyage_output.py inventory -h
.venv/bin/python update_voyage_output.py convert -h
.venv/bin/python update_voyage_output.py run -h
```
