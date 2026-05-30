from dotenv import dotenv_values

config = dotenv_values(".env")

gemini_api_key = config['GEMINI_API_KEY']
deepseek_api_key = config['DEEPSEEK_API_KEY']

