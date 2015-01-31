import json
import logging
import optparse
import os
import socket
import sys
import threading
import time
import Queue

import feedparser
from gi.repository import Notify
import requests

__version__ = '0.1'

SOCKET_TIMEOUT = 30

CACHE_DIR = os.path.join(os.getenv('HOME'), '.githubnotifier', 'cache')
CONFIG_FILE = os.path.join(os.getenv('HOME'), '.githubnotifier', 'config.cfg')

GITHUB_BLOG_URL = 'https://github.com/blog.atom'
GITHUB_BLOG_USER = 'GitHub Blog'
GITHUB_URL = 'https://github.com/'

notification_queue = Queue.Queue()


def cache_info(filename, data):
    info_cache = os.path.abspath(os.path.join(CACHE_DIR, filename))
    fp = open(info_cache, 'w')
    fp.write(data)
    fp.close()


def get_cached_data_or_none(filename):
    if does_cached_file_exist(filename) is False:
        return None
    info_cache = os.path.abspath(os.path.join(CACHE_DIR, filename))
    fp = open(info_cache, 'r')
    info = fp.read()
    fp.close()
    return json.loads(info)


def does_cached_file_exist(filename):
    info_cache = os.path.abspath(os.path.join(CACHE_DIR, filename))
    return os.path.exists(info_cache)


class GithubInfo():
    @staticmethod
    def get_basic_user_info(username):
        username = username.split(' ')[0]
        user_filename = '{}.json'.format(username)
        user = get_cached_data_or_none(filename=user_filename)
        if not user:
            # Fetch userinfo from github
            response = requests.get('https://api.github.com/users/{}'.format(username))
            user = response.json()
            cache_info(filename=user_filename, data=response.content)

            if not response.ok:
                # Create a 'fake' user object in case of network errors
                user = {'login': username}

        avatar_filename = '{}.jpg'.format(username)
        if does_cached_file_exist(avatar_filename) is False:
            # Fetch the user's gravatar
            gravatar_url = user.get('avatar_url', 'http://www.gravatar.com/avatar/?s=48')
            response = requests.get(gravatar_url)
            if response.ok:
                cache_info(avatar_filename, response.content)

        user['avatar_path'] = os.path.abspath(os.path.join(CACHE_DIR, avatar_filename))
        return user

    @staticmethod
    def get_organizations(username):
        username = username.split(' ')[0]
        organizations_filename = '{}_orgs.json'.format(username)
        organizations = get_cached_data_or_none(filename=organizations_filename)
        if organizations is None:
            # Fetch organizations info from github
            response = requests.get('https://api.github.com/users/{}/orgs'.format(username))
            organizations = response.json()
            cache_info(filename=organizations_filename, data=response.content)

            if not response.ok:
                # Create empty organization list in case of network errors
                organizations = []

        return [org['login'] for org in organizations]


class UserConfig():
    @staticmethod
    def get_github_config():
        fp = os.popen('git config --get github.user')
        user = fp.readline().strip()
        fp.close()

        fp = os.popen('git config --get github.token')
        token = fp.readline().strip()
        fp.close()

        return (user, token)


class GithubFeedUpdatherThread(threading.Thread):
    def __init__(self, user, token, interval, max_items, hyperlinks, blog,
                 important_authors, important_projects, blacklist_authors,
                 blacklist_projects, organizations, blacklist_organizations):
        threading.Thread.__init__(self)

        self.logger = logging.getLogger('github-notifier')

        self.feeds = [
            'http://github.com/{}.private.atom?token={}'.format(user, token),
            'http://github.com/{}.private.actor.atom?token={}'.format(user, token),
        ]
        if blog:
            self.logger.info('Observing the GitHub Blog')
            self.feeds.append(GITHUB_BLOG_URL)

        self.interval = interval
        self.max_items = max_items
        self.hyperlinks = hyperlinks
        self._seen = {}
        self.important_authors = important_authors
        self.important_projects = important_projects
        self.blacklist_authors = blacklist_authors
        self.blacklist_projects = blacklist_projects
        self.organizations = organizations
        self.blacklist_organizations = blacklist_organizations
        self.list_important_authors = []
        self.list_important_projects = []
        self.list_blacklist_authors = []
        self.list_blacklist_projects = []
        self.list_blacklist_organizations = []
        self.users_organizations = GithubInfo.get_organizations(username=user)

        list_organizations = self.users_organizations
        # Blacklist the organizations
        if self.organizations and self.blacklist_organizations:
            list_organizations = filter(
                lambda x: x not in self.list_blacklist_organizations,
                self.users_organizations
            )

        # Add all the organizations feeds to the feeds
        for organization in list_organizations:
            self.feeds.append('https://github.com/organizations/{}/{}.private.atom?token={}'.format(organization, user, token))

    def run(self):
        while True:
            self.update_feeds(self.feeds)
            time.sleep(self.interval)

    def process_feed(self, feed_url):
        self.logger.info('Fetching feed {}'.format(feed_url))
        feed = feedparser.parse(feed_url)

        notifications = []
        for entry in feed.entries:
            if not entry['id'] in self._seen:

                if feed_url is GITHUB_BLOG_URL:
                    entry['author'] = GITHUB_BLOG_USER

                notifications.append(entry)
                self._seen[entry['id']] = 1

        return notifications

    def update_feeds(self, feeds):
        notifications = []
        for feed_url in feeds:
            notifications.extend(self.process_feed(feed_url))

        notifications.sort(key=lambda e: e['updated'])

        notifications = notifications[-self.max_items:]

        users = {}
        l = []
        found_author = False
        found_project = False

        for item in notifications:
            user = users.get(item.get('author'))
            if user is None:
                users[item['author']] = GithubInfo.get_basic_user_info(username=item['author'])
            user = users[item['author']]

            if self.hyperlinks and 'link' in item:
                # simple heuristic: use the second word for the link
                parts = item['title'].split(' ')
                if len(parts) > 1:
                    parts[1] = '<a href="{}">{}</a>'.format(item['link'], parts[1])
                message = ' '.join(parts)
            else:
                message = item['title']
            self.logger.info(user)
            n = {
                'title': user.get('name', user['login']),
                'message': message,
                'icon': user['avatar_path']
            }

            # Check for GitHub Blog entry
            if item['author'] == GITHUB_BLOG_USER:
                self.logger.info('Found GitHub Blog item entry')
                n['icon'] = os.path.abspath('octocat.png')

            # Check for important project entry
            if self.important_projects:
                found_project = any(
                    self.important_repository(item['link'], project) for project in self.list_important_projects
                )

            # Check for important author entry
            if self.important_authors:
                found_author = item['authors'][0]['name'] in self.list_important_authors

            # Report and add only relevant entries
            if self.important_authors and found_author:
                self.logger.info('Found important author item entry')
                l.append(n)
            elif self.important_projects and found_project:
                self.logger.info('Found important project item entry')
                l.append(n)
            elif not self.important_authors and not self.important_projects:

                ignore_author = False
                ignore_project = False

                # Check to see if entry is a blacklisted author
                if self.blacklist_authors and item['authors'][0]['name'] in self.list_blacklist_authors:
                    self.logger.info('Ignoring blacklisted author entry')
                    ignore_author = True

                # Check to see if entry is a blacklisted project
                if self.blacklist_projects:
                    if any(self.important_repository(item['link'], project) for project in self.list_blacklist_projects):
                        self.logger.info('Ignoring blacklisted project entry')
                        break

                if not ignore_author and not ignore_project:
                    self.logger.info('Found item entry')
                    l.append(n)
            else:
                self.logger.info('Ignoring non-important item entry')

        notification_queue.put(l)

    def important_repository(self, link, project):
        link_parts = link.split('/')

        if len(link_parts) > 4:  # Ensures that the link has enough information
            project_parts = project.split('/')

            # Acquire the parts of the project (account for unique/global repo)
            project_owner = None
            if len(project_parts) == 2:
                project_owner = project_parts[0]
                project = project_parts[1]
            owner_from_link = link_parts[3]
            project_from_link = link_parts[4]

            # True if projects match when there is no owner, or if all match
            return project == project_from_link and (
                not project_owner or project_owner == owner_from_link
            )
        else:
            return False


def display_notifications(display_timeout=None):
    while True:
        try:
            items = notification_queue.get_nowait()
            for i in items:
                n = Notify.Notification.new(i['title'], i['message'], i['icon'])
                if display_timeout is not None:
                    n.set_timeout(display_timeout * 1000)
                n.show()

            notification_queue.task_done()
        except Queue.Empty:
            break

    return True


def parse_and_validate_args():
    parser = optparse.OptionParser()
    parser.add_option('-i', '--update-interval', action='store', type='int', dest='interval', default=300, help='set the feed update interval (in seconds)')
    parser.add_option('-m', '--max-items', action='store', type='int', dest='max_items', default=3, help='maximum number of items to be displayed per update')
    parser.add_option('-t', '--display-timeout', action='store', type='int', dest='timeout', help='set the notification display timeout (in seconds)')
    parser.add_option('-b', '--blog', action='store_true', dest='blog', default=False, help='enable notifications from GitHub\'s blog')
    parser.add_option('-a', '--important_authors', action='store_true', dest='important_authors', default=False, help='only consider notifications from important authors')
    parser.add_option('-o', '--organizations', action='store_true', dest='organizations', default=True, help='consider notifications of all user\'s organizations')
    parser.add_option('-k', '--blacklist_organizations', action='store_true', dest='blacklist_organizations', default=False, help='filter out blacklisted organizations')
    parser.add_option('-p', '--important_projects', action='store_true', dest='important_projects', default=False, help='only consider notifications from important projects')
    parser.add_option('-u', '--blacklist_authors', action='store_true', dest='blacklist_authors', default=False, help='filter out blacklisted authors')
    parser.add_option('-r', '--blacklist_projects', action='store_true', dest='blacklist_projects', default=False, help='filter out blacklisted projects')
    parser.add_option('-n', '--new-config', action='store_true', dest='new_config', default=False, help='create a new config.cfg at ~/.githubnotifier/')
    parser.add_option('-v', '--verbose', action='store_true', dest='verbose', default=False, help='enable verbose logging')
    (options, args) = parser.parse_args()

    # Create logger
    logger = logging.getLogger('github-notifier')
    handler = logging.StreamHandler()

    if options.verbose:
        logger.setLevel(logging.INFO)
    else:
        logger.setLevel(logging.WARNING)

    formatter = logging.Formatter(
        '[%(levelname)s] %(asctime)s\n%(message)s',
        datefmt='%d %b %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    if options.interval <= 0:
        logger.error('The update interval must be > 0')
        sys.exit(1)

    if options.max_items <= 0:
        logger.error('The maximum number of items must be > 0')
        sys.exit(1)

    if not os.path.isdir(CACHE_DIR):
        logger.warning('Making the cache directory {0}'.format(CACHE_DIR))
        os.makedirs(CACHE_DIR)

    if not os.path.isfile(CONFIG_FILE) or options.new_config:
        logger.warning('Making the config file {0}'.format(CONFIG_FILE))
        config_file = open(CONFIG_FILE, 'w')
        config_file.write('[important]  # Separated by commas, projects (can be either <user>/<project> or <project>)\n')
        config_file.write('authors=\nprojects=')
        config_file.write('\n[blacklist]  # Separated by commas, projects (can be either <user>/<project> or <project>)\n')
        config_file.write('authors=\nprojects=')
        config_file.write('\norganizations=')
        config_file.close()

    if not Notify.init('github-notifier'):
        logger.error('Couldn\'t initialize notify')
        sys.exit(1)

    server_caps = Notify.get_server_caps()
    if 'body-hyperlinks' in server_caps:
        logger.info('github-notifier is capable of using hyperlinks')
        hyperlinks = True
    else:
        logger.info('github-notifier is not capable of using hyperlinks')
        hyperlinks = False

    (user, token) = UserConfig.get_github_config()
    if not user or not token:
        logger.error(
            '''Could not get GitHub username and token from git config
            you can run
               $git config --global github.user <username>
            and
               $git config --global github.token <token>
            to configure it.\n
            for more information about token check the link https://help.github.com/articles/creating-an-access-token-for-command-line-use''')
        sys.exit(1)

    return {
        'user': user,
        'token': token,
        'hyperlinks': hyperlinks,
        'options': options
    }


def main():
    socket.setdefaulttimeout(SOCKET_TIMEOUT)

    validated_data = parse_and_validate_args()

    # Start a new thread to check for feed updates
    upd = GithubFeedUpdatherThread(
        validated_data.get('user'),
        validated_data.get('token'),
        validated_data.get('options').interval,
        validated_data.get('options').max_items,
        validated_data.get('hyperlinks'),
        validated_data.get('options').blog,
        validated_data.get('options').important_authors,
        validated_data.get('options').important_projects,
        validated_data.get('options').blacklist_authors,
        validated_data.get('options').blacklist_projects,
        validated_data.get('options').organizations,
        validated_data.get('options').blacklist_organizations
    )
    upd.setDaemon(True)
    upd.start()

    DISPLAY_INTERVAL = 1  # In seconds
    while True:
        display_notifications(validated_data.get('options').timeout)
        time.sleep(DISPLAY_INTERVAL)


if __name__ == '__main__':
    main()
