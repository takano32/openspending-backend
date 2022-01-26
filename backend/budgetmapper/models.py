import sys
from io import BufferedIOBase, RawIOBase
from typing import Any

import pykakasi
import shortuuidfield
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.text import slugify
from polymorphic.models import PolymorphicModel
from rest_framework.authtoken.models import Token


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_auth_token(sender, instance=None, created=False, **kwargs):
    if created:
        Token.objects.create(user=instance)


class CurrentDateTimeField(models.DateTimeField):
    def __init__(self, *args, **kwargs):
        super(CurrentDateTimeField, self).__init__(
            *args, **dict(kwargs, default=timezone.now, editable=False, null=False)
        )


class AutoUpdateCurrentDateTimeField(CurrentDateTimeField):
    def pre_save(self, model_instance, add):
        val = timezone.now()
        setattr(model_instance, self.attname, val)
        return val


kks = pykakasi.kakasi()


def jp_sluggify(name: str) -> str:
    return slugify("-".join(d["hepburn"] for d in kks.convert(name)))


class JpSlugField(models.SlugField):
    def pre_save(self, model_instance, add):
        val = getattr(model_instance, self.attname)
        if val is None or len(val) == 0:
            val = jp_sluggify(model_instance.name)
            setattr(model_instance, self.attname, val)
        return val


class NameField(models.TextField):
    def __init__(self, *args, **kwargs):
        super(NameField, self).__init__(*args, **dict(kwargs, null=True, db_index=True))


class IdField(shortuuidfield.ShortUUIDField):
    def __init__(self, *args, **kwargs):
        super(IdField, self).__init__(*args, **dict(kwargs, editable=False))


class PkField(IdField):
    def __init__(self, *args, **kwargs):
        super(PkField, self).__init__(*args, **dict(kwargs, primary_key=True))


class BudgetAmountField(models.FloatField):
    ...


class LatitudeField(models.FloatField):
    def __init__(self, *args, **kwargs):
        super(LatitudeField, self).__init__(
            *args, **dict(kwargs, validators=[MinValueValidator(-90.0), MaxValueValidator(90.0)])
        )


class LongitudeField(models.FloatField):
    def __init__(self, *args, **kwargs):
        super(LongitudeField, self).__init__(
            *args, **dict(kwargs, validators=[MinValueValidator(0.0), MaxValueValidator(180.0)])
        )


class Government(models.Model):
    id = PkField()
    name = NameField()
    slug = JpSlugField(unique=True)
    latitude = LatitudeField()
    longitude = LongitudeField()
    created_at = CurrentDateTimeField()
    updated_at = AutoUpdateCurrentDateTimeField()


class ClassificationSystem(models.Model):
    id = PkField()
    name = NameField()
    slug = JpSlugField(unique=True)
    created_at = CurrentDateTimeField()
    updated_at = AutoUpdateCurrentDateTimeField()

    @property
    def roots(self) -> models.QuerySet:
        return Classification.objects.filter(parent=None)

    @property
    def leaves(self) -> models.QuerySet:
        return Classification.objects.filter(
            ~models.Exists(Classification.objects.filter(classification_system=self, parent=models.OuterRef("pk"))),
            classification_system=self,
        )


class Classification(models.Model):
    id = PkField()
    name = NameField()
    code = models.CharField(max_length=64, null=True)
    classification_system = models.ForeignKey(ClassificationSystem, on_delete=models.CASCADE)
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True)
    created_at = CurrentDateTimeField()
    updated_at = AutoUpdateCurrentDateTimeField()

    @property
    def level(self) -> int:
        if self.parent is None:
            return 0
        return self.parent.level + 1


class Budget(models.Model):
    id = PkField()
    name = NameField()
    slug = JpSlugField(unique=True)
    year = models.IntegerField()
    subtitle = models.TextField()
    classification_system = models.ForeignKey(ClassificationSystem, on_delete=models.CASCADE, db_index=True, null=False)
    government = models.ForeignKey(Government, on_delete=models.CASCADE, db_index=True, null=False)
    created_at = CurrentDateTimeField()
    updated_at = AutoUpdateCurrentDateTimeField()

    def get_value_of(self, classification: Classification) -> float:
        if self.classification_system != classification.classification_system:
            raise ValueError
        val = BudgetItemBase.objects.get(budget=self, classification=classification)
        if val is not None:
            return val.value
        else:
            return sum(self.get_value_of(c) for c in Classification.objects.filter(parent=classification))


class BudgetItemBase(PolymorphicModel):
    id = PkField()
    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, db_index=True, null=False)
    classification = models.ForeignKey(Classification, on_delete=models.CASCADE, db_index=True, null=False)
    created_at = CurrentDateTimeField()
    updated_at = AutoUpdateCurrentDateTimeField()

    @property
    def value(self) -> float:
        raise NotImplementedError

    class Meta:
        unique_together = ("budget", "classification")


class AtomicBudgetItem(BudgetItemBase):
    amount = BudgetAmountField()

    @property
    def value(self) -> float:
        return float(self.amount)


class MappedBudgetItem(BudgetItemBase):
    mapped_budget = models.ForeignKey(Budget, db_index=True, on_delete=models.CASCADE, null=False)
    mapped_classifications = models.ManyToManyField(Classification, related_name="mapping_classifications")

    @property
    def value(self) -> float:
        return sum(self.mapped_budget.get_value_of(c) for c in self.mapped_classifications.all())


class Blob(models.Model):
    id = PkField()
    name = NameField()
    created_at = CurrentDateTimeField()
    updated_at = AutoUpdateCurrentDateTimeField()

    @classmethod
    def write(cls, data: RawIOBase, name: str = None, chunk_size: int = 65536) -> None:
        instance = cls(name=name)
        instance.save()
        idx = 0
        while True:
            buf = data.read(chunk_size)
            if len(buf) == 0:
                break
            BlobChunk(blob=instance, index=idx, body=buf).save()
            idx += 1


class BlobChunk(models.Model):
    id = PkField()
    blob = models.ForeignKey(Blob, on_delete=models.CASCADE, db_index=False, null=False)
    index = models.PositiveIntegerField(db_index=False)
    body = models.BinaryField(db_index=False)

    class Meta:
        unique_together = ("blob", "index")


class BlobReader(BufferedIOBase):
    def __init__(self, blob: Blob):
        self._fp = BlobChunk.objects.filter(blob=blob).order_by("index")
        self._buffer = b''
        self._gen = self._next()

    def _next(self) -> BlobChunk:
        for d in self._fp:
            yield d

    def read(self, size: int = -1) -> bytes:
        while size == -1 or len(self._buffer) < size:
            try:
                self._buffer += next(self._gen).body
            except StopIteration:
                break
        if size >= 0:
            retval = self._buffer[:size]
            self._buffer = self._buffer[size:]
        else:
            retval = self._buffer
            self._buffer = b""
        return retval
