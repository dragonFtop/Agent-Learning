@echo off
rem Start LiteLLM proxy: Anthropic format -> local Ollama
cd /d %~dp0
litellm --config litellm_config.yaml --port 4000
