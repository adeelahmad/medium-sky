import requests
import bs4
from text_analyzer import page_analyzer, stats_to_text, counts, profile_to_text
import re
import markdown
import backoff
import pickle
from dotenv import load_dotenv
import os
from subprocess import check_output
import json
import validators

# load environment variables from .env file
load_dotenv()

INLINE_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
FOOTNOTE_LINK_TEXT_RE = re.compile(r'\[([^\]]+)\]\[(\d+)\]')
FOOTNOTE_LINK_URL_RE = re.compile(r'\[(\d+)\]:\s+(\S+)')

EXCLUDE_URLS = 'unsplash|shutterstock|freepi|\.png|\.jpeg|\.gif|\.jpg'

API_URL = "https://medium2.p.rapidapi.com"

HEADERS = {
    "X-RapidAPI-Key": os.environ.get('RAPID_API'),
    "X-RapidAPI-Host": "medium2.p.rapidapi.com"
}


def find_md_links(md: str) -> list:
    """ Return dict of links in markdown """

    links = list(INLINE_LINK_RE.findall(md))
    footnote_links = dict(FOOTNOTE_LINK_TEXT_RE.findall(md))
    footnote_urls = dict(FOOTNOTE_LINK_URL_RE.findall(md))

    for key in footnote_links.keys():
        links.append((footnote_links[key], footnote_urls[footnote_links[key]]))

    return links


def load_js_state(soup, state: str = 'window.__PRELOADED_STATE__') -> dict:
    """
    Load JS state from a soup object
    """
    s = [x for x in soup.find_all('script') if state in str(x)][0]
    with open('temp.js', 'w', encoding="utf-8") as f:
        f.write('window = {};\n' +
                s.text.strip() +
                f';\nprocess.stdout.write(JSON.stringify({state}));')
    window_init_state = check_output(['node', 'temp.js'])
    os.remove('temp.js')
    rs = json.loads(window_init_state)
    return rs


def get_user_id_unofficial(user: str) -> dict:
    """
    Unofficial method to get user info
    """
    user_url = f"https://medium.com/@{user}"
    response = requests.get(user_url, headers=HEADERS)
    soup = bs4.BeautifulSoup(response.content, features="lxml")
    preload_state = load_js_state(soup, state='window.__PRELOADED_STATE__')
    user_id = preload_state['client']['routingEntity']['id']
    appolo_state = load_js_state(soup, state='window.__APOLLO_STATE__')
    social_stats = appolo_state[f'User:{user_id}']['socialStats']
    return {"user_id": user_id, "social_stats": social_stats}


def get_article_markdown_unofficial(url: str) -> list:
    """
    Unofficial method to get article markdown. Only works with articles without a paywall
    """

    rs = []
    article_response = requests.get(url)
    soup = bs4.BeautifulSoup(article_response.content, features="lxml")
    preload_state = load_js_state(soup, state='window.__APOLLO_STATE__')

    # iterate over each key in the __APOLLO_STATE__ object and extract the markups and text
    for k in preload_state.keys():
        markups = preload_state[k].get('markups', [])
        markups_text = preload_state[k].get('text')

        # iterate over each markup and extract the start, end, href, and text
        for markup in markups:
            if markup:
                start = markup.get('start')
                end = markup.get('end')
                href = markup.get("href")
                text = markups_text[start:end]
                if href:
                    if validators.url(href):
                        rs.append((text, href))

    return rs


def get_user_id(user: str) -> str:
    """
    Get user_id using Medium API
    """
    url = f"{API_URL}/user/id_for/{user}"
    response = requests.get(url, headers=HEADERS)
    response.json()
    user_id = response.json()["id"]
    return user_id


def get_user_info(user_id: str) -> dict:
    """
    Get user_info using Medium API
    """
    url = f"{API_URL}/user/{user_id}"
    response = requests.get(url, headers=HEADERS)
    return response.json()


def get_user_articles(user_id: str) -> list:
    """
    Get user article_ids using Medium API
    """
    url = f"{API_URL}/user/{user_id}/articles"
    response = requests.request("GET", url, headers=HEADERS)
    articles = response.json()['associated_articles']
    return articles


def get_article_markdown(article_id: str) -> str:
    """
    Get article markdown using Medium API
    """
    url = f"{API_URL}/article/{article_id}/markdown"
    response = requests.request("GET", url, headers=HEADERS)
    return response.json()["markdown"]


@backoff.on_exception(backoff.expo,
                      requests.exceptions.RequestException,
                      max_tries=3,
                      jitter=None)
def get_article_url(url: str) -> str:
    return requests.get(url).url


def get_article_content(article_id: str) -> dict:
    markdown_text = get_article_markdown(article_id)
    links = find_md_links(markdown_text)
    return {"links": links, "markdown_text": markdown_text}


class MediumArticles:
    def __init__(self, username: str, articles_limit: int = 0, reset: bool = False):
        self.username = username
        self.user_words = []
        self.articles_limit = articles_limit
        self.reset = reset

    def get_all_articles(self) -> dict:
        """
        Get all user's articles and analyze them.
        If pickle file with data exists use the file else use the API (except the case you specify reset=True).
        File has the following format <username>_<articles_limit>.
        If <articles_limit>=0 then download all articles.
        Articles are saved before any NLP analysis (page_analyzer()) so you can adjust page_analyzer() to your needs.
        """
        file_name = f'data/{self.username}_{self.articles_limit}.pickle'
        # If file exists, load data from file
        if os.path.exists(file_name) and not self.reset:
            print("using the local file...")
            with open(file_name, 'rb') as f:
                data_to_keep = pickle.load(f)
        # If file does not exist, use Medium API
        else:
            print("using the api...")
            # Get user info
            user_id = get_user_id(self.username)
            user_info = get_user_info(user_id)
            article_ids = get_user_articles(user_id)

            # Parse articles
            data_to_keep = dict()
            data_to_keep["user"] = {"id": user_id, "info": user_info}
            data_to_keep["articles"] = []

            main_counter = 1
            for article_id in article_ids:
                print(f"getting article {article_id}...")
                article_content = get_article_content(article_id)
                article_url = get_article_url(f"https://{user_id}.medium.com/{article_id}")
                data_to_keep["articles"].append(
                    {"id": article_id,
                     "url": article_url,
                     "links": article_content["links"],
                     "markdown": article_content["markdown_text"]
                     }
                )
                if self.articles_limit:
                    if main_counter >= self.articles_limit:
                        break
                main_counter += 1

            with open(file_name, 'wb') as f:
                pickle.dump(data_to_keep, f)

        # Analyze articles
        for article_content in data_to_keep["articles"]:
            html = markdown.markdown(article_content["markdown"])
            soup = bs4.BeautifulSoup(html, features="lxml")
            stats = page_analyzer(soup)
            article_content["stats_dict"] = stats
            article_content["stats"] = stats_to_text(stats)

            self.user_words.extend(stats["words"])

        # Aggregate Statistics
        aggs = counts(self.user_words)
        data_to_keep["user"]["profile"] = profile_to_text(data_to_keep, aggs)
        return data_to_keep
