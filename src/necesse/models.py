from django.db import models
from django.contrib.postgres.search import SearchVector
from django.contrib.postgres.indexes import GinIndex

class NCServerLog(models.Model):
  timestamp = models.DateTimeField()
  log_path = models.CharField(max_length=500, null=True)
  hostname = models.CharField(max_length=100, default='asean-mt-server')
  tag = models.CharField(max_length=100, default='necesse')
  text = models.TextField()
  event_processed = models.BooleanField(default=False)

  class Meta:
    verbose_name = "Server Log"
    verbose_name_plural = "Server Logs"
    constraints = [
      models.UniqueConstraint(
        fields=['timestamp', 'text'],
        name='nc_unique_event_log_entry'
      )
    ]
    indexes = [
      GinIndex(
        SearchVector('text', config='english'),
        name='nc_log_text_search_idx',
      )
    ]

