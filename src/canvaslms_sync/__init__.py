#!/usr/bin/env python3

VERSION = "1.0.0"

import argparse
import canvasapi
import configparser
from   datetime import datetime
from   functools import cached_property
import logging
import os
from   pathlib import Path, PurePath
import re
import urllib.parse

logger = logging.getLogger(__name__)

class CanvasSyncException(Exception):
    pass

class CourseDoesNotExist(CanvasSyncException):
    def __init__(self, course_id):
        self.course_id = course_id

    def __str__(self):
        return f"The course with ID {self.course_id} does not exist."

class CanvasSyncer(object):
    # Configuration options loaded from a file
    config = None

    # The API token to use with Canvas
    canvas_api_token = None

    # A regex matching URLS of folders in Canvas, or just courses
    re_folder_url = re.compile(r'^(?P<host>https?://[^/]+)/courses/(?P<course_id>\d+)(?:/files/folder/(?P<path>.*))?$')

    # The root URL of the Canvas instance, e.g. "https://myuni.instructure.com"
    host = None

    # The ID of the course to be synced with
    course_id = None

    # An instance of canvasapi.Canvas
    canvas = None

    # An instance of canvasapi.Course
    course = None

    def __init__(self, options):
        self.options = options
        self.canvas_api_token = self.options.canvas_api_token
        self.ignore_hidden_files = not self.options.include_hidden

        self.load_config(options.config)

        if self.canvas_api_token is None:
            raise CanvasSyncException("You didn't give a Canvas API token.")

    def load_config(self, config_file_path):
        """
            Load the configuration data.
        """
        self.config = configparser.ConfigParser()
        self.config.read(config_file_path)

        if self.canvas_api_token is None:
            try:
                self.canvas_api_token = self.config.get('Canvas','canvas_api_token')
            except configparser.Error:
                pass

    def init(self, url):
        """ 
            Initialise the connection to Canvas

            Arguments:
                * url : str - The URL of the Canvas course, or a folder in that course.
        """

        m = self.re_folder_url.match(url)
        if not m:
            raise CanvasSyncException(f"This URL doesn't look like the URL of a Canvas course or folder: {url}")

        host = m.group('host')
        course_id = int(m.group('course_id'))

        self.canvas = canvasapi.Canvas(host, self.canvas_api_token)
        try:
            self.course = self.canvas.get_course(course_id)
        except canvasapi.exceptions.ResourceDoesNotExist:
            raise CourseDoesNotExist(course_id)

        try:
            folder_path = m.group('path')
        except IndexError:
            folder_path = ''

        target_folder = self.get_folder(PurePath(urllib.parse.unquote(folder_path)))

        return target_folder

    @cached_property
    def remote_folders(self):
        return list(self.course.get_folders())

    def get_folder(self, path):
        """
            Get a folder in the course's Files section corresponding to the given path.

            Arguments:
                * path : pathlib.PurePath - The path to the folder. The empty path is the top level of the Files section.

            Returns:
                * canvasapi.Folder
        """
        root = None
        for a in self.remote_folders:
            if a.full_name == 'course files':
                root = a
            p = PurePath(a.full_name).relative_to(PurePath('course files'))
            if p == path:
                return a

        # folder mustn't exist, so create it:
        cf = CachedFolder(root)
        return cf.get_folder(path).folder
        
    def scan_folder(folder):
        files = {}
        folders = {}
        for file in folder.get_files():
            files[file.filename] = file
            
        for subfolder in folder.get_folders():
            folders[folder.name] = scan_folder(subfolder)
            
        return {
            'folder': folder,
            'files': files,
            'folders': folders,
        }

    def sync_folder(self, remote_url, local_root):
        target_folder = self.init(remote_url)
        cf = CachedFolder(target_folder)
        logger.info(f"Synchronising {local_root} with {remote_url}")

        for root, folders, files in os.walk(local_root):
            root_path = Path(root)
            if self.ignore_hidden_files and any(p.startswith('.') for p in root_path.parts):
                continue
            for filename in files:
                if self.ignore_hidden_files and filename.startswith('.'):
                    continue
                local_path = root_path / filename
                p = Path(root).relative_to(local_root)
                remote_folder = cf.get_folder(p)
                remote_file = remote_folder.files.get(filename)
                local_mtime = datetime.fromtimestamp(local_path.stat().st_mtime)
                remote_mtime = datetime.strptime(remote_file.modified_at, "%Y-%m-%dT%H:%M:%SZ") if remote_file is not None else None
                if remote_file is None or remote_mtime < local_mtime:
                    remote_folder.upload(local_path)

        logger.info(f"Done!")

class CachedFolder(object):
    """
        A wrapper around ``canvasapi.Folder``, caching the lists of files and folders.
    """
    def __init__(self, folder):
        """
            Arguments:
                * folder: canvasapi.Folder
        """
        self.folder = folder
        
    def __str__(self):
        return self.folder.full_name
        
    @cached_property
    def files(self):
        """
            The files in this folder.
            A dictionary mapping filenames to ``canvasapi.File`` objects.
        """
        _files = {}
        for file in self.folder.get_files():
            _files[file.filename] = file
        return _files
        
    @cached_property
    def folders(self):
        """
            The subfolders in this folder.
            A dictionary mapping folder names to ``CachedFolder`` objects.
        """
        _folders = {}
        for folder in self.folder.get_folders():
            _folders[folder.name] = CachedFolder(folder)
        return _folders
    
    def get_folder(self, name):
        """
            Get a folder under this one.

            Arguments:
                * name : str or os.PathLike

            Returns:
                CachedFolder
        """
        if isinstance(name, os.PathLike):
            f = self
            for part in name.parts:
                f = f.get_folder(part)
            return f

        folder = self.folders.get(name)
        if folder is None:
            logger.info(f"Creating remote folder {Path(self.folder.full_name) / name}")
            folder = CachedFolder(self.folder.create_folder(name))
            self.folders[name] = folder

        return folder
    
    def upload(self, file):
        """
            Upload a file to this folder.

            Arguments:
                * file : str or file
        """
        logger.info(f"Uploading {file} to {self}")

        uploaded, response = self.folder.upload(file)

        if not uploaded:
            raise CanvasSyncException(f"The file {file} was not uploaded successfully.")

        del self.files

        return response

def cli():
    parser = argparse.ArgumentParser(description='''Synchronise a local folder with a Canvas course's files section. You must give a Canvas API token, either with the -t parameter, or in a credentials.ini file.''')
    parser.add_argument('local_folder', type=str,
                        help='The local folder to synchronise.')
    parser.add_argument('canvas_url', type=str,
                        help='The URL of the Canvas folder to synchronise with.')
    parser.add_argument('-t', '--canvas_api_token', type=str,
                        help='Your Canvas API token.')
    parser.add_argument('-c', '--config', type=str, default='credentials.ini',
                        help='The config file to use.')
    parser.add_argument('--include-hidden', action='store_true',
                        help='Upload hidden files and folders')

    args = parser.parse_args()

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(ch)

    try:
        syncer = CanvasSyncer(args)
        syncer.sync_folder(args.canvas_url, args.local_folder)
    except CanvasSyncException as e:
        logger.error(f"Error: {e}")
    except canvasapi.exceptions.InvalidAccessToken as e:
        logger.error(f"Canvas API Error: Your API token is invalid")
    except canvasapi.exceptions.CanvasException as e:
        logger.error(f"Canvas API Error: {e}")


if __name__ == '__main__':
    cli()
