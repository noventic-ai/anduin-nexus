import json
import os
import re
import asyncio
import logging
import pandas as pd
from typing import List, Dict, Any, Optional, Union

try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

def locate_json_string_body_from_string(content: str) -> list | None:
    """
    Locate the JSON string body from a string
    This code is borrowed from LightRag
    https://github.com/HKUDS/LightRAG/tree/main
    """
    if not isinstance(content, str):
        return None
    
    try:
        # Find JSON arrays that are properly formatted
        json_pattern = re.compile(r'\[\s*(?:"[^"]*"|\d+|true|false|null|\{[^{}]*\}|\[[^\[\]]*\])(?:\s*,\s*(?:"[^"]*"|\d+|true|false|null|\{[^{}]*\}|\[[^\[\]]*\]))*\s*\]', re.DOTALL)
        matches = json_pattern.findall(content)
        
        for match in matches:
            try:
                # Validate if it's valid JSON
                parsed = json.loads(match)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                continue
        
        # Alternative approach: find "json" tag and extract following JSON
        json_tag_pattern = re.search(r'json\s*\[.*?\]', content, re.DOTALL)
        if json_tag_pattern:
            json_str = json_tag_pattern.group(0).replace("json", "", 1).strip()
            try:
                parsed = json.loads(json_str)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
                
    except Exception:
        pass
        
    return None

def safe_unicode_decode(content: str) -> str:
    '''
    This code is borrowed from LightRag
    https://github.com/HKUDS/LightRAG/tree/main
    '''
    # Regular expression to find all Unicode escape sequences of the form \uXXXX
    unicode_escape_pattern = re.compile(r"\\u([0-9a-fA-F]{4})")
    
    # Function to replace the Unicode escape with the actual character
    def replace_unicode_escape(match):
        # Convert the matched hexadecimal value into the actual Unicode character
        return chr(int(match.group(1), 16))
    
    # Perform the substitution
    decoded_content = unicode_escape_pattern.sub(
        replace_unicode_escape, content
    )
    new_decoded_content=re.sub(r"\n","",decoded_content)
    return new_decoded_content
    
def convert_response_to_json(response: str) -> list | None:
    """
    Convert LLM response to JSON data with proper validation
    """
    if not isinstance(response, str):
        return None
    
    parsed_data = locate_json_string_body_from_string(response)
    if parsed_data is None:
        # Try to parse the entire response as JSON if no specific pattern found
        try:
            parsed_data = json.loads(response)
            if not isinstance(parsed_data, list):
                parsed_data = [parsed_data]
        except json.JSONDecodeError:
            return None
    
    return parsed_data

    

def chunking_by_word_size(
    content: str,
    split_by_character: str | None = None,
    split_by_character_only: bool = False,
    overlap_word_size: int = 128,
    max_word_size: int = 1024,
) -> list[str]:
    '''
    split the text into chunks if long text is used
    This code is borrowed from LightRag
    https://github.com/HKUDS/LightRAG/tree/main
    '''
    results: list[str] = []
    if split_by_character:
        raw_chunks = content.split(split_by_character)
        new_chunks = []
        if split_by_character_only:
            for chunk in raw_chunks:
                new_chunks.append( chunk)
        else:
            for chunk in raw_chunks:
                if len(chunk) > max_word_size:
                    for start in range(
                        0, len(chunk), max_word_size - overlap_word_size
                    ):
                        chunk_content = chunk[start : start + overlap_word_size]
                        new_chunks.append( chunk_content)
                else:
                    new_chunks.append( chunk)
        for index, (_len, chunk) in enumerate(new_chunks):
            results.append(chunk.strip())
    else:
        for index, start in enumerate(
            range(0, len(content), max_word_size - overlap_word_size)
        ):
            chunk_content = content[start : start + max_word_size]
            results.append(chunk_content)
    return results

def standarized_llm_df(df: pd.DataFrame) -> pd.DataFrame:
    '''
    if df includes non-standard gene name or disease name, a transformation is need
    Need to be updated
    '''
    return df


def _normalize_openai_content(content: Any) -> str:
    """Normalize OpenAI message content to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                continue
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _get_openai_client() -> "AsyncOpenAI":
    if not OPENAI_AVAILABLE:
        raise ImportError("openai package is not installed. Install it or switch to another backend")
    return AsyncOpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL"),
    )


async def openai_chat_task(semaphore: asyncio.Semaphore, model: str, content: List[Dict[str, str]], temperature: float = 0.7) -> Dict[str, Any]:
    async with semaphore:
        client = _get_openai_client()
        response = await client.chat.completions.create(
            model=model,
            messages=content,
            temperature=temperature,
        )
        message_content = ""
        if response.choices:
            message_content = _normalize_openai_content(response.choices[0].message.content)
        return {"message": {"content": message_content}}

async def chat_llm(model: str, content: List[List[Dict[str, str]]], temperature: Union[List[float], float], semaphore_limit: int = 2, backend: str = "openai") -> List[str]:
    """
    Async function to handle multiple LLM chat requests with proper error handling
    Supports only the OpenAI-compatible backend.
    """
    if not content or not isinstance(content, list):
        return []
    
    # Ensure temperature is a list with matching length
    if isinstance(temperature, (int, float)):
        temperature = [temperature] * len(content)
    elif isinstance(temperature, list) and len(temperature) != len(content):
        temperature = [temperature[0]] * len(content)

    backend = backend.lower().strip()
    if backend != "openai":
        raise ValueError("Only the 'openai' backend is supported in this build")
    
    semaphore=asyncio.Semaphore(semaphore_limit)
    
    async def safe_chat_task(i: int) -> str:
        try:
            response = await openai_chat_task(semaphore, model, content[i], temperature[i])
            
            if response and 'message' in response and 'content' in response['message']:
                return remove_thinking(response['message']['content'])
            return ""
        except Exception as e:
            logging.error(f"Error in chat_llm task {i} with backend {backend}: {str(e)}")
            return ""
    
    tasks = [safe_chat_task(i) for i in range(len(content))]
    responses = await asyncio.gather(*tasks, return_exceptions=False)
    return responses


async def openai_embed_task(semaphore: asyncio.Semaphore, model: str, content: str) -> List[float]:
    async with semaphore:
        client = _get_openai_client()
        response = await client.embeddings.create(model=model, input=content)
        return response.data[0].embedding
        
#async def embed_llm(model: str, content: List[str]) -> List[List[float]]:
async def embed_llm(model: str, content: List[str], semaphore_limit: int = 2, backend: str = "openai") -> List[List[float]]:
    """
    Async function to handle multiple LLM embedding requests
    Supports only the OpenAI-compatible backend.
    """
    if backend.lower().strip() != "openai":
        raise ValueError("Only the 'openai' backend is supported in this build")

    semaphore=asyncio.Semaphore(semaphore_limit)
    tasks = [openai_embed_task(semaphore, model, cc) for cc in content]
    return await asyncio.gather(*tasks)

def read_doc_by_word_number(input_file: str, max_word_size: int, overlap_word_size: int) -> Dict[str, Any]:
    try:
        import textract
    except ImportError as exc:
        raise ImportError(
            "textract is required for read_doc_by_word_number(). Install textract to use document parsing."
        ) from exc

    input_text = textract.process(input_file).decode("utf8")
    input_text=re.sub(r'\n', ' ', input_text, count=0, flags=0)
    out=chunking_by_word_size(input_text, overlap_word_size=overlap_word_size, max_word_size=max_word_size)
    return {'abstract': out[0], 'main_text': out[1:], 'meta_info':{'paper_id': input_file, 'weight': 0}}


def remove_thinking(input_string: str) -> str:
    input_string=re.sub(r'<thinking>.*?</thinking>', '', input_string, flags=re.DOTALL)
    input_string=re.sub(r'<think>.*?</think>', '', input_string, flags=re.DOTALL)
    input_string=re.sub(r'\s*</start_of_turn>\s*', '', input_string, flags=re.DOTALL)
    input_string=re.sub(r'\s*</end_of_turn>\s*', '', input_string, flags=re.DOTALL).strip()
    return input_string

