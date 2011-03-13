from django.core.exceptions import ImproperlyConfigured
from django.db.backends.signals import connection_created
from django.conf import settings
from django.utils.functional import wraps

from pymongo.connection import Connection
from pymongo.collection import Collection

from .creation import DatabaseCreation
from .client import DatabaseClient
from .utils import CollectionDebugWrapper

from djangotoolbox.db.base import (
    NonrelDatabaseFeatures,
    NonrelDatabaseWrapper,
    NonrelDatabaseValidation,
    NonrelDatabaseIntrospection,
    NonrelDatabaseOperations
)

from datetime import datetime

class DatabaseFeatures(NonrelDatabaseFeatures):
    string_based_auto_field = True
    supports_dicts = True

class DatabaseOperations(NonrelDatabaseOperations):
    compiler_module = __name__.rsplit('.', 1)[0] + '.compiler'

    def max_name_length(self):
        return 254

    def check_aggregate_support(self, aggregate):
        import aggregations
        try:
            getattr(aggregations, aggregate.__class__.__name__)
        except AttributeError:
            raise NotImplementedError("django-mongodb-engine does not support %r "
                                      "aggregates" % type(aggregate))

    def sql_flush(self, style, tables, sequence_list):
        """
        Returns a list of SQL statements that have to be executed to drop
        all `tables`. No SQL in MongoDB, so just drop all tables here and
        return an empty list.
        """
        tables = self.connection.collection_names()
        for table in tables:
            if table.startswith('system.'):
                # do not try to drop system collections
                continue
            self.connection.database.drop_collection(table)
        return []

    def value_to_db_date(self, value):
        if value is None:
            return None
        return datetime(value.year, value.month, value.day)

    def value_to_db_time(self, value):
        if value is None:
            return None
        return datetime(1, 1, 1, value.hour, value.minute, value.second,
                                 value.microsecond)

class DatabaseValidation(NonrelDatabaseValidation):
    pass

class DatabaseIntrospection(NonrelDatabaseIntrospection):
    def table_names(self):
        return self.connection.collection_names()

    def sequence_list(self):
        # Only required for backends that support ManyToMany relations
        pass

def requires_connection(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        if not self._connected:
            self._connect()
        return method(self, *args, **kwargs)
    return wrapper

class DatabaseWrapper(NonrelDatabaseWrapper):
    def __init__(self, *args, **kwargs):
        self.collection_class = kwargs.pop('collection_class', Collection)
        super(DatabaseWrapper, self).__init__(*args, **kwargs)
        self.features = DatabaseFeatures(self)
        self.ops = DatabaseOperations(self)
        self.client = DatabaseClient(self)
        self.creation = DatabaseCreation(self)
        self.introspection = DatabaseIntrospection(self)
        self.validation = DatabaseValidation(self)
        self._connected = False

    @requires_connection
    def get_collection(self, name, **kwargs):
        collection = self.collection_class(self.database, name, **kwargs)
        if settings.DEBUG:
            collection = CollectionDebugWrapper(collection)
        return collection

    @requires_connection
    def drop_database(self, name):
        return self._connection.drop_database(name)

    @requires_connection
    def collection_names(self):
        return self.database.collection_names()

    def _connect(self):
        settings = self.settings_dict.copy()
        db_name = settings.pop('NAME')
        host = settings.pop('HOST', None)
        port = settings.pop('PORT', None)
        user = settings.pop('USER', None)
        password = settings.pop('PASSWORD')
        options = settings.pop('OPTIONS', {})

        if port:
            try:
                port = int(port)
            except ValueError:
                raise ImproperlyConfigured("If set, PORT must be an integer "
                                           "(got %r instead)" % port)
        else:
            # If PORT is not specified Django sets it to '' which makes
            # PyMongo fail, so make sure it's None in that case
            port = None

        self.operation_flags = options.pop('OPERATIONS', {})
        if not any(k in ['save', 'delete', 'update'] for k in self.operation_flags):
            # flags apply to all operations
            flags = self.operation_flags
            self.operation_flags = {'save' : flags, 'delete' : flags, 'update' : flags}

        # Compatibility to version < 0.4
        if 'SAFE_INSERTS' in settings:
            self.operation_flags['save']['safe'] = settings['SAFE_INSERTS']
        if 'WAIT_FOR_SLAVES' in settings:
            self.operation_flags['save']['w'] = settings['WAIT_FOR_SLAVES']

        # lower-case all remaining OPTIONS
        for key, value in options.items():
            options[key.lower()] = options.pop(key)

        try:
            self._connection = Connection(host=host, port=port, **options)
            self.database = self._connection[db_name]
        except TypeError:
            import sys
            exc_info = sys.exc_info()
            raise ImproperlyConfigured, exc_info[1], exc_info[2]

        if user and password:
            if not self.database.authenticate(user, password):
                raise ImproperlyConfigured("Invalid username or password")

        self._add_serializer()
        self._connected = True
        connection_created.send(sender=self.__class__, connection=self)

    def _add_serializer(self):
        for option in ['MONGODB_AUTOMATIC_REFERENCING',
                       'MONGODB_ENGINE_ENABLE_MODEL_SERIALIZATION']:
            if getattr(settings, option, False):
                from .serializer import TransformDjango
                self.database.add_son_manipulator(TransformDjango())
                return
