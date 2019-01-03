from django.conf import settings
from django.db import models, connection, transaction

from .postgresql_backend.base import check_schema_name
from .signals import schema_post_sync, schema_needs_sync, schema_pre_drop
from .utils import schema_exists, create_schema, drop_schema, get_domain_model


class TenantMixin(models.Model):
    """
    All tenant models must inherit this class.
    """

    auto_create_schema = True
    """
    Set this flag to false on a parent class if you don't want the schema
    to be automatically created upon save.
    """

    auto_drop_schema = False
    """
    USE THIS WITH CAUTION!
    Set this flag to true on a parent class if you want the schema to be
    automatically deleted if the tenant row gets deleted.
    """

    schema_name = models.CharField(max_length=63, unique=True, validators=[check_schema_name])

    domain_url = None
    """
    Leave this as None. Stores the current domain url so it can be used in the logs
    """

    class Meta:
        abstract = True

    def __enter__(self):
        """
        Syntax sugar which helps in celery tasks, cron jobs, and other scripts

        Usage:
            with Tenant.objects.get(schema_name='test') as tenant:
                # run some code in tenant test
            # run some code in previous tenant (public probably)
        """
        self.previous_schema = connection.schema_name
        self.activate()

    def __exit__(self, exc_type, exc_val, exc_tb):
        connection.set_schema(self.previous_schema)

    def activate(self):
        """
        Syntax sugar that helps at django shell with fast tenant changing

        Usage:
            Tenant.objects.get(schema_name='test').activate()
        """
        connection.set_schema(self.schema_name)

    @classmethod
    def deactivate(cls):
        """
        Syntax sugar, return to public schema

        Usage:
            test_tenant.deactivate()
            # or simpler
            Tenant.deactivate()
        """
        connection.set_schema_to_public()

    def save(self, verbosity=1, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)

        if is_new and self.auto_create_schema:
            try:
                self.create_schema(check_if_exists=True, verbosity=verbosity)
                schema_post_sync.send(sender=TenantMixin, tenant=self.serializable_fields())
            except Exception:
                # We failed creating the tenant, delete what we created and re-raise the exception
                self.delete(force_drop=True)
                raise
        elif is_new:
            # Although we are not using the schema functions directly, the signal might be registered by a listener
            schema_needs_sync.send(sender=TenantMixin, tenant=self.serializable_fields())
        elif not is_new and self.auto_create_schema and not schema_exists(self.schema_name):
            # Create schemas for existing models, deleting only the schema on failure
            try:
                self.create_schema(check_if_exists=True, verbosity=verbosity)
                schema_post_sync.send(sender=TenantMixin, tenant=self.serializable_fields())
            except Exception:
                # We failed creating the schema, delete what we created and re-raise the exception
                self.drop_schema()
                raise

    def delete(self, force_drop=False, *args, **kwargs):
        """
        Deletes this row. Drops the tenant's schema if the attribute auto_drop_schema is True.
        """
        if force_drop or self.auto_drop_schema:
            schema_pre_drop.send(sender=TenantMixin, tenant=self.serializable_fields())
            self.drop_schema()
        super().delete(*args, **kwargs)

    def serializable_fields(self):
        """
        In certain cases the user model isn't serializable so you may want to only send the id.
        """
        return self

    def create_schema(self, check_if_exists=False, sync_schema=True, verbosity=1):
        """
        Creates the schema 'schema_name' for this tenant.
        """
        return create_schema(self.schema_name, check_if_exists, sync_schema, verbosity)

    def drop_schema(self):
        """
        Drops the schema.
        """
        return drop_schema(self.schema_name)

    def get_primary_domain(self):
        """
        Returns the primary domain of the tenant.
        """
        try:
            domain = self.domains.get(is_primary=True)
            return domain
        except get_domain_model().DoesNotExist:
            return None


class DomainMixin(models.Model):
    """
    All models that store the domains must inherit this class.
    """

    domain = models.CharField(max_length=253, unique=True, db_index=True)
    tenant = models.ForeignKey(
        settings.TENANTS["public"]["TENANT_MODEL"], db_index=True, related_name="domains", on_delete=models.CASCADE
    )

    is_primary = models.BooleanField(default=True)

    @transaction.atomic
    def save(self, *args, **kwargs):
        domain_list = self.__class__.objects.filter(tenant=self.tenant, is_primary=True).exclude(pk=self.pk)
        self.is_primary = self.is_primary or (not domain_list.exists())
        if self.is_primary:
            domain_list.update(is_primary=False)
        super().save(*args, **kwargs)

    class Meta:
        abstract = True