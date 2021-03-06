# -*- coding: utf-8 -*-
import re
import hmac
import hashlib

import psycopg2
from flask import current_app as app

from .database import Database


class InvalidInstanceName(Exception):

    def __init__(self, name):
        self.args = ["%s is a invalid name."]


class InstanceAlreadyExists(Exception):

    def __init__(self, name):
        self.args = ["Instance %s already exists." % name]


class InstanceNotFound(Exception):

    def __init__(self, name):
        self.args = ["Instance %s is not found." % name]


class DatabaseCreationError(Exception):
    pass


def generate_password(string, host):
    hm = hmac.new(app.config['SALT'], digestmod=hashlib.sha1)
    hm.update(string)
    hm.update(host)
    return hm.hexdigest()


def generate_user(string, host):
    if len(string) > 10:
        string = string[:10]
    string += generate_password(string, host)[:6]
    return string


def generate_group(string):
    if len(string) > 10:
        string = string[:10]
        string += '_group'
    return string


def canonicalize_db_name(name):
    if re.search(r"[\W\s]", name) is not None:
        suffix = hashlib.sha1(name).hexdigest()[:10]
        name = re.sub(r"[\W\s]", "_", name) + suffix
    return name


class ClusterManager(object):

    def __init__(self,
                 host='localhost',
                 port=5432,
                 user='postgres',
                 password='',
                 public_host=None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self._public_host = public_host
        self.dbs = {}

    @property
    def public_host(self):
        if self._public_host:
            return self._public_host
        return self.host

    def db(self, name=None):
        if name is None:
            name = 'postgres'  # default database
        if name not in self.dbs:
            self.dbs[name] = Database(name,
                                      self.user,
                                      self.password,
                                      self.host,
                                      self.port)
        return self.dbs[name]

    def create_database(self, name, encoding=None):
        with self.db().autocommit() as cursor:
            grpsql = 'CREATE ROLE %(group)s WITH NOLOGIN'
            dbsql = 'CREATE DATABASE %(name)s'
            ownsql = 'ALTER DATABASE %(name)s OWNER TO %(group)s'
            if encoding is not None:
                dbsql += ' ENCODING %(encoding)s'
            group = generate_group(name)
            context = {'name': name,
                       'group': group,
                       'encoding': encoding}
            cursor.execute(grpsql % context)
            cursor.execute(dbsql % context)
            cursor.execute(ownsql % context)

    def drop_database(self, name):
        with self.db().autocommit() as cursor:
            group = generate_group(name)
            cursor.execute("DROP DATABASE %s" % name)
            cursor.execute("DROP ROLE %s" % group)

    def create_user(self, database, host):
        with self.db(database).autocommit() as cursor:
            username = generate_user(database, host)
            password = generate_password(database, host)
            group = generate_group(database)
            sql = "CREATE ROLE %s WITH LOGIN PASSWORD %%s IN ROLE %s"
            cursor.execute(sql % (username, group), (password, ))
            return username, password

    def drop_user(self, database, host):
        with self.db(database).autocommit() as cursor:
            username = generate_user(database, host)
            cursor.execute("DROP ROLE %s" % username)

    def is_up(self, database):
        return self.db(database).ping()


class Instance(object):

    __tablename__ = 'instance'

    STATE_CHOICES = (
        ("pending", "pending"),
        ("running", "running"),
        ("error", "error"),
    )

    def __init__(self, name):
        self.name = canonicalize_db_name(name)
        self.shared = True
        self.state = 'pending'

    @classmethod
    def create(cls, name):
        # if instance.name in settings.RESERVED_NAMES:
        #     raise InvalidInstanceName(name=instance.name)
        with app.db.transaction() as cursor:
            instance = Instance(name)
            cursor.execute('SELECT 1 FROM %s WHERE name=%%s' %
                           cls.__tablename__, (instance.name, ))
            if cursor.fetchone():
                raise InstanceAlreadyExists(name=instance.name)
            instance.shared = True
            try:
                instance.cluster_manager.create_database(instance.name)
            except psycopg2.ProgrammingError as e:
                if e.args and 'already exists' in e.args[0]:
                    raise InstanceAlreadyExists(name=instance.name)
                raise
            instance.state = 'running'
            cursor.execute('INSERT INTO %s (name, state, shared) '
                           'VALUES (%%s, %%s, %%s)' % cls.__tablename__,
                           (instance.name, instance.state, instance.shared))
            return instance

    @classmethod
    def retrieve(cls, name):
        with app.db.transaction() as cursor:
            instance = Instance(name)
            cursor.execute('SELECT name, state, shared FROM %s '
                           'WHERE name=%%s' % cls.__tablename__,
                           (instance.name, ))
            try:
                name, state, shared = cursor.fetchone()
            except TypeError:
                raise InstanceNotFound(name=instance.name)
            instance.state = state
            instance.shared = shared
            return instance

    @classmethod
    def delete(cls, name):
        instance = cls.retrieve(name)
        with app.db.transaction() as cursor:
            instance.cluster_manager.drop_database(instance.name)
            cursor.execute('DELETE FROM %s WHERE name=%%s' %
                           cls.__tablename__, (instance.name, ))

    def create_user(self, host):
        return self.cluster_manager.create_user(self.name, host)

    def drop_user(self, host):
        return self.cluster_manager.drop_user(self.name, host)

    @property
    def public_host(self):
        return self.cluster_manager.public_host

    @property
    def port(self):
        return self.cluster_manager.port

    def is_up(self):
        return (self.state == "running" and
                self.cluster_manager.is_up(self.name))

    @property
    def cluster_manager(self):
        config = app.config
        if self.shared:
            host = config['SHARED_HOST']
            port = config['SHARED_PORT']
            admin = config['SHARED_ADMIN']
            password = config['SHARED_ADMIN_PASSWORD']
            public_host = config['SHARED_PUBLIC_HOST']
        else:
            raise NotImplementedError(
                'Currently only shared host is supported')
        return ClusterManager(host=host,
                              port=port,
                              user=admin,
                              password=password,
                              public_host=public_host)
