# GitHub to Codeberg mirror

Mirror all repositories from a GitHub organization to Codeberg, preserving visibility settings.

## Features

- Mirrors all org repositories (public and private)
- Preserves repository visibility (private repos stay private)
- Creates repositories on Codeberg if they don't exist
- Updates existing mirrors incrementally
- Dry-run mode for testing (--dry-run)
- Single repository can be mirrored (--repo)
- Can continue where left off (--skip-existing)

## Requirements

- Linux with git installed
- [uv](https://github.com/astral-sh/uv) package manager
- GitHub Personal Access Token (classic) with `repo` scope
- Codeberg Access Token with repository permissions
- Codeberg organization must exist (create manually first)

## Usage

```shell
$ uv run mirror.py --help
usage: mirror.py [-h] [--config CONFIG] [--dry-run] [--repo REPO] [--debug] [--check-token] [--skip-existing]

Mirror GitHub repos to Codeberg                      

options:                                                                                                                                                      
  -h, --help       show this help message and exit                                                                                                            
  --config CONFIG  Path to config file                                                                                                                        
  --dry-run        Show what would be done without making changes                                                                                             
  --repo REPO      Mirror only this specific repository                                                                                                       
  --debug          Enable debug output                                                                                                                        
  --check-token    Only check token permissions, don't mirror                                                                                                 
  --skip-existing  Skip repos that already exist on Codeberg (useful for initial sync)                                                                        
```


## TODO:
- [ ] make systemd service + timer
- [ ] deploy on hilde workstation
