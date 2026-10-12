"""
Gemini AI Brain Module
Direct Google Gemini API integration for lightning-fast quiz solving,
multimodal vision analysis, coding lab solutions, and autonomous navigation.
"""

import os
import json
import base64
import re
import urllib.request
import urllib.error
from typing import Optional, Dict, Any, List

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-1.5-flash",
    "gemini-flash-latest",
    "gemini-3.8-flash",
    "gemini-2.5-flash"
]


class GeminiBrain:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or self._load_key()
        self.active_model = DEFAULT_MODELS[0]
        self._discovered_models: List[str] = []

    def _load_key(self) -> str:
        # 1. Environment variable
        env_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if env_key:
            return env_key

        # 2. Local config.json
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("gemini_api_key", "").strip()
            except Exception:
                pass

        # 3. .env file
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("GEMINI_API_KEY="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass

        return ""

    def set_api_key(self, key: str) -> bool:
        """Sets and persists the Gemini API key in config.json."""
        self.api_key = (key or "").strip()
        config_data = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
            except Exception:
                config_data = {}

        config_data["gemini_api_key"] = self.api_key
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=2)
            return True
        except Exception:
            return False

    def has_key(self) -> bool:
        return bool(self.api_key and len(self.api_key) > 10)

    def _discover_models(self) -> List[str]:
        """Queries Google API to dynamically discover all models that support generateContent."""
        if not self.has_key():
            return []
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}&pageSize=50"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = []
                for m in data.get("models", []):
                    if "generateContent" in m.get("supportedGenerationMethods", []):
                        name = m.get("name", "").replace("models/", "")
                        models.append(name)
                # Prioritize flash models
                flash_models = [m for m in models if "flash" in m.lower() and "tts" not in m.lower() and "image" not in m.lower()]
                other_models = [m for m in models if m not in flash_models]
                self._discovered_models = flash_models + other_models
                return self._discovered_models
        except Exception:
            return []

    def get_candidate_models(self) -> List[str]:
        candidates = [self.active_model]
        for m in DEFAULT_MODELS:
            if m not in candidates:
                candidates.append(m)
        for m in self._discovered_models:
            if m not in candidates:
                candidates.append(m)
        return candidates

    def test_connection(self) -> Dict[str, Any]:
        """Tests the API key by pinging the Gemini models."""
        if not self.has_key():
            return {"success": False, "error": "No API key configured."}

        models_to_test = self.get_candidate_models()
        tried_discover = False

        while models_to_test:
            model = models_to_test.pop(0)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
            payload = {
                "contents": [{"parts": [{"text": "Reply with only the word OK"}]}],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 250}
            }
            try:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=12) as response:
                    resp_json = json.loads(response.read().decode("utf-8"))
                    candidates = resp_json.get("candidates", [])
                    if not candidates:
                        continue
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts).strip()
                    if text:
                        self.active_model = model
                        return {"success": True, "model": model, "response": text}
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore")
                if e.code in (400, 404, 429, 500, 503):
                    if not tried_discover and not models_to_test:
                        tried_discover = True
                        discovered = self._discover_models()
                        models_to_test.extend([d for d in discovered if d not in DEFAULT_MODELS])
                    continue
                return {"success": False, "error": f"HTTP {e.code}: {err_body}"}
            except Exception as e:
                return {"success": False, "error": str(e)}

        return {"success": False, "error": "None of the candidate Gemini models responded."}

    def _call_api(self, contents: List[Dict[str, Any]], system_instruction: Optional[str] = None,
                  temperature: float = 0.2, max_tokens: int = 4096) -> str:
        """Core call to the Google Generative Language REST API."""
        if not self.has_key():
            raise ValueError("Gemini API key is not configured. Please enter your key in settings.")

        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens
            }
        }
        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }

        last_error = None
        models_to_try = self.get_candidate_models()
        tried_discover = False

        while models_to_try:
            model = models_to_try.pop(0)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
            try:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=25) as response:
                    resp_json = json.loads(response.read().decode("utf-8"))
                    candidates = resp_json.get("candidates", [])
                    if not candidates:
                        continue
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts)
                    self.active_model = model
                    return text.strip()
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="ignore")
                last_error = f"HTTP {e.code}: {err_text}"
                if e.code in (404, 400, 429, 500, 503):
                    if not tried_discover and not models_to_try:
                        tried_discover = True
                        discovered = self._discover_models()
                        models_to_try.extend([d for d in discovered if d not in DEFAULT_MODELS])
                    continue
                break
            except Exception as e:
                last_error = str(e)
                break

        raise RuntimeError(f"Gemini API request failed: {last_error}")

    def ask_text(self, prompt: str, system_instruction: Optional[str] = None, temperature: float = 0.2) -> str:
        """Sends a text-based prompt to Gemini."""
        contents = [{"parts": [{"text": prompt}]}]
        return self._call_api(contents, system_instruction=system_instruction, temperature=temperature)

    def ask_vision(self, prompt: str, image_bytes_or_base64: Any, mime_type: str = "image/jpeg",
                   system_instruction: Optional[str] = None, temperature: float = 0.1) -> str:
        """Sends a multimodal prompt (image + text) to Gemini Vision."""
        if isinstance(image_bytes_or_base64, bytes):
            b64_str = base64.b64encode(image_bytes_or_base64).decode("utf-8")
        elif isinstance(image_bytes_or_base64, str):
            b64_str = image_bytes_or_base64.split(",")[-1]
        else:
            raise ValueError("Image must be bytes or base64 string")

        contents = [{
            "parts": [
                {
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": b64_str
                    }
                },
                {"text": prompt}
            ]
        }]
        return self._call_api(contents, system_instruction=system_instruction, temperature=temperature)

    def solve_mcq(self, question_text: str, options: List[str], screenshot_bytes: Optional[bytes] = None) -> Dict[str, Any]:
        """
        Solves an MCQ quiz question.
        Uses multimodal vision if screenshot is provided, otherwise text prompt.
        Guarantees structured output: { 'selected_option': 'A', 'explanation': '...' }
        """
        prompt = (
            "You are an expert examination solver answering a technical multiple-choice question.\n"
            "Analyze the question and all options thoroughly.\n\n"
            f"Question:\n{question_text}\n\n"
            f"Options:\n" + "\n".join(f"{chr(65+i)}. {opt}" for i, opt in enumerate(options)) + "\n\n"
            "INSTRUCTIONS:\n"
            "1. Determine the single most accurate, correct option.\n"
            "2. Return ONLY a valid JSON object in this exact format, with no markdown code blocks:\n"
            '{"selected_option": "A", "option_index": 0, "explanation": "Brief rationale"}\n'
            "(selected_option must be A, B, C, D, etc., and option_index must be 0-based integer)."
        )

        sys_inst = "You are a precise test-taking AI assistant. Always return pure JSON."

        try:
            if screenshot_bytes:
                raw_resp = self.ask_vision(prompt, screenshot_bytes, mime_type="image/jpeg", system_instruction=sys_inst)
            else:
                raw_resp = self.ask_text(prompt, system_instruction=sys_inst)
        except Exception as e:
            return {"selected_option": "A", "option_index": 0, "error": str(e), "explanation": "Fallback due to API error"}

        # Extract JSON
        clean_resp = re.sub(r"^```(?:json)?\s*", "", raw_resp.strip(), flags=re.IGNORECASE)
        clean_resp = re.sub(r"\s*```$", "", clean_resp.strip())

        try:
            parsed = json.loads(clean_resp)
            opt_letter = str(parsed.get("selected_option", "A")).strip().upper()[:1]
            if opt_letter not in "ABCDEFGH":
                opt_letter = "A"
            idx = parsed.get("option_index")
            if idx is None or not isinstance(idx, int):
                idx = ord(opt_letter) - ord("A")
            return {
                "selected_option": opt_letter,
                "option_index": idx,
                "explanation": parsed.get("explanation", ""),
                "raw_response": raw_resp
            }
        except Exception:
            # Fallback regex search for letter
            m = re.search(r'\b([A-D])\b', raw_resp.upper())
            letter = m.group(1) if m else "A"
            return {
                "selected_option": letter,
                "option_index": ord(letter) - ord("A"),
                "explanation": raw_resp[:100],
                "raw_response": raw_resp
            }

    def solve_coding(self, problem_title: str, problem_desc: str, starter_code: str = "",
                     language: str = "python", screenshot_bytes: Optional[bytes] = None) -> str:
        """Generates optimal, tested solution code for a coding lab/editor problem."""
        prompt = (
            f"You are a master software engineer solving a {language.upper()} programming challenge.\n"
            f"Problem Title: {problem_title}\n\n"
            f"Problem Description:\n{problem_desc}\n\n"
        )
        if starter_code:
            prompt += f"Starter Code:\n```{language}\n{starter_code}\n```\n\n"

        prompt += (
            "REQUIREMENTS:\n"
            "1. Provide the complete, working solution code that passes all test cases and edge cases.\n"
            "2. Read input from standard input if required, or implement the exact expected class/function.\n"
            "3. Return ONLY the code inside a single code block ```python ... ``` (or the matching language).\n"
            "4. Do NOT include conversational explanations outside the code block."
        )

        sys_inst = f"You are an automated code generator for ByteXL labs. Output only executable {language} code."

        if screenshot_bytes:
            raw_resp = self.ask_vision(prompt, screenshot_bytes, mime_type="image/jpeg", system_instruction=sys_inst)
        else:
            raw_resp = self.ask_text(prompt, system_instruction=sys_inst)

        # Extract code from code block
        code_match = re.search(r"```(?:\w+)?\n([\s\S]*?)```", raw_resp)
        if code_match:
            return code_match.group(1).strip()
        return raw_resp.strip()

    def decide_navigation(self, current_url: str, page_title: str, visible_buttons: List[str],
                          headings: List[str], screenshot_bytes: Optional[bytes] = None) -> Dict[str, Any]:
        """
        Autonomous Agent Decision:
        Inspects the page context (and screenshot) and determines the best next action
        to advance course completion towards 100%.
        """
        prompt = (
            "You are an autonomous course assistant navigating an e-learning platform (ByteXL).\n"
            "Our goal is to complete all course units, reading materials, quizzes, and labs to reach 100% completion.\n\n"
            f"Current URL: {current_url}\n"
            f"Page Title: {page_title}\n"
            f"Visible Headings: {headings[:10]}\n"
            f"Visible Buttons/Links: {visible_buttons[:25]}\n\n"
            "INSTRUCTIONS:\n"
            "Analyze the page and return ONLY a valid JSON object deciding the next best action:\n"
            "{\n"
            '  "page_type": "course_catalog" | "units_list" | "curriculum_module" | "reading_topic" | "quiz_landing" | "coding_lab",\n'
            '  "action": "click_button" | "expand_accordion" | "mark_complete" | "advance_unit" | "solve_quiz" | "solve_lab",\n'
            '  "target_button_text": "Exact text of the button or item to click",\n'
            '  "reason": "Clear 1-sentence rationale"\n'
            "}"
        )

        sys_inst = "You are a web navigation AI planner. Return only pure JSON."

        try:
            if screenshot_bytes:
                raw = self.ask_vision(prompt, screenshot_bytes, mime_type="image/jpeg", system_instruction=sys_inst)
            else:
                raw = self.ask_text(prompt, system_instruction=sys_inst)

            clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
            clean = re.sub(r"\s*```$", "", clean.strip())
            return json.loads(clean)
        except Exception as e:
            return {
                "page_type": "unknown",
                "action": "click_button",
                "target_button_text": "Continue Learning",
                "reason": f"Fallback navigation: {e}"
            }


    @staticmethod
    def normalize_acronym(text: str) -> str:
        """Standardizes casing and expands known engineering acronyms (e.g. 'rdms'/'rdbms' -> 'RDBMS')."""
        if not text:
            return ""
        clean = text.strip()
        lower = clean.lower()
        mapping = {
            "rdbms": "RDBMS",
            "rdms": "RDBMS",
            "dbms": "DBMS",
            "dsa": "DSA",
            "os": "OS",
            "cn": "CN",
            "ai": "AI",
            "genai": "GenAI",
            "ml": "ML",
            "oop": "OOP",
            "oops": "OOPS",
            "toc": "TOC",
            "cd": "CD",
            "wt": "WT",
            "az900": "AZ-900",
            "az-900": "AZ-900"
        }
        return mapping.get(lower, clean)

    def get_search_terms(self, target_query: str) -> List[str]:
        """
        Returns a prioritized list of search terms to query ByteXL's catalog search bar.
        For acronyms like 'RDBMS', returns both the acronym and key full-text terms
        ('Relational Database', 'Database Management', 'SQL') so ByteXL's client-side filter succeeds.
        """
        if not target_query:
            return []
        norm = self.normalize_acronym(target_query)
        lower = target_query.strip().lower()

        ACRONYM_SEARCH = {
            "rdbms": ["Relational Database", "Database Management", "RDBMS", "DBMS", "Database", "SQL"],
            "rdms": ["Relational Database", "Database Management", "RDBMS", "DBMS", "Database", "SQL"],
            "dbms": ["Database Management", "DBMS", "Database", "SQL"],
            "dsa": ["Data Structures", "Algorithms", "DSA"],
            "os": ["Operating Systems", "Operating System", "OS"],
            "cn": ["Computer Networks", "Computer Networking", "CN"],
            "ai": ["Artificial Intelligence", "AI"],
            "genai": ["Generative AI", "Cloud Security", "GenAI"],
            "ml": ["Machine Learning", "ML"],
            "cloud": ["Cloud Security", "Cloud Computing"],
            "900": ["AZ-900", "Azure", "Cloud Security"],
            "az900": ["AZ-900", "Azure"],
            "az-900": ["AZ-900", "Azure"],
            "oop": ["Object Oriented", "OOP", "Java"],
            "oops": ["Object Oriented", "OOPS", "Java"],
            "toc": ["Theory of Computation", "Automata", "TOC"],
            "cd": ["Compiler Design", "CD"],
            "wt": ["Web Technologies", "Web Development", "WT"]
        }

        terms = [norm]
        if lower in ACRONYM_SEARCH:
            for term in ACRONYM_SEARCH[lower]:
                if term not in terms:
                    terms.append(term)
        elif lower != norm.lower() and norm not in terms:
            terms.append(norm)

        return terms

    def is_matching_page_course(self, target_query: str, page_text: str) -> bool:
        """
        Returns True if the given page URL, title, or body text matches the target course,
        understanding acronyms (e.g. 'RDBMS' matches 'Relational Database Management').
        """
        if not target_query or not page_text:
            return False
        q_norm = self.normalize_acronym(target_query).lower()
        text_lower = page_text.lower()
        if q_norm in text_lower:
            return True
        terms = self.get_search_terms(target_query)
        for term in terms:
            if term.lower() in text_lower:
                return True
        return False

    def match_course(self, target_query: str, available_courses: List[str]) -> Optional[str]:
        """
        Intelligently resolves a user's course search query (including acronyms like RDBMS/rdms,
        typos, abbreviations, or partial names) to the exact matching course title from ByteXL.
        """
        if not target_query or not available_courses:
            return None

        q_raw = target_query.strip()
        q_clean = re.sub(r'[^a-zA-Z0-9\s]', '', q_raw).lower()
        q_tokens = [t for t in q_clean.split() if t]

        if not q_tokens:
            return None

        # 1. Fast Dictionary for common engineering course acronyms & aliases
        ACRONYMS = {
            "rdbms": ["relational database", "database management", "dbms", "sql", "rdms"],
            "rdms": ["relational database", "rdbms", "database management", "dbms", "sql"],
            "dbms": ["database management system", "database", "rdbms", "sql"],
            "dsa": ["data structures", "algorithms", "data structures and algorithms"],
            "os": ["operating system", "operating systems"],
            "cn": ["computer networks", "computer networking", "networking"],
            "ai": ["artificial intelligence", "genai", "generative ai"],
            "genai": ["generative ai", "cloud security (genai)", "generative artificial intelligence"],
            "ml": ["machine learning"],
            "cloud": ["cloud security", "cloud computing", "aws", "azure"],
            "900": ["az-900", "az 900", "azure", "cloud security"],
            "az900": ["az-900", "az 900", "azure"],
            "oop": ["object oriented", "oops", "java", "c++"],
            "oops": ["object oriented", "oop", "java", "c++"],
            "se": ["software engineering"],
            "toc": ["theory of computation", "automata"],
            "cd": ["compiler design"],
            "wt": ["web technologies", "web development"]
        }

        # 1a. Direct exact or substring match
        for course in available_courses:
            c_clean = re.sub(r'[^a-zA-Z0-9\s]', '', course).lower()
            if q_clean == c_clean or q_clean in c_clean or c_clean in q_clean:
                return course

        # 1b. Acronym expansion match (e.g. user typed 'rdms' or 'rdbms' or 'dsa')
        for token in q_tokens:
            synonyms = ACRONYMS.get(token, [])
            for syn in synonyms:
                for course in available_courses:
                    c_clean = re.sub(r'[^a-zA-Z0-9\s]', '', course).lower()
                    if syn in c_clean:
                        return course

        # 1c. Initials match (e.g. 'RDBMS' matches 'Relational DataBase Management System')
        def get_initials(text: str) -> str:
            words = re.findall(r'[a-zA-Z0-9]+', text)
            return "".join(w[0].lower() for w in words if w)

        for course in available_courses:
            initials = get_initials(course)
            if q_clean == initials or q_clean in initials:
                return course

        # 1d. Token overlap match (at least 60% of query words appear in course title)
        best_overlap_course = None
        max_overlap_count = 0
        for course in available_courses:
            c_tokens = set(re.findall(r'[a-zA-Z0-9]+', course.lower()))
            overlap = sum(1 for t in q_tokens if t in c_tokens)
            if overlap > max_overlap_count and overlap >= len(q_tokens) * 0.5:
                max_overlap_count = overlap
                best_overlap_course = course

        if best_overlap_course:
            return best_overlap_course

        # 2. AI Brain Semantic & Typo Matching with Gemini API
        if self.has_key():
            prompt = (
                f"The user wants to navigate to the course: '{target_query}'.\n"
                f"The user may have typed an acronym (e.g. 'RDBMS'), a typo (e.g. 'rdms'), a code, or an informal name.\n\n"
                f"Here are the actual course titles available on ByteXL:\n"
                + "\n".join(f"- {c}" for c in available_courses) + "\n\n"
                "INSTRUCTIONS:\n"
                "Identify which course title from the list best matches the user's intended target.\n"
                "If a good match exists, return its exact title from the list. If none match, return null.\n"
                "Respond ONLY with a JSON object: {\"matched_title\": \"<exact title from list or null>\", \"reason\": \"<brief reason>\"}"
            )
            try:
                raw = self.ask_text(prompt, system_instruction="You are a course title matcher. Return pure JSON only.")
                clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
                clean = re.sub(r"\s*```$", "", clean.strip())
                data = json.loads(clean)
                matched = data.get("matched_title")
                if matched:
                    for c in available_courses:
                        if c.strip().lower() == matched.strip().lower():
                            return c
            except Exception:
                pass

        return None


# Global singleton instance
brain = GeminiBrain()

