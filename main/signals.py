from django.dispatch import receiver
from django.db.models.signals import pre_save
from main.models import Comment


@receiver(pre_save, sender=Comment)
def removing_duplicate_comment(sender, instance, **kwargs):
    comment = Comment.objects.filter(
        user=instance.user,
        place_id=instance.place_id,
        note=instance.note,
    )
    if comment:
        comment.last().delete()
