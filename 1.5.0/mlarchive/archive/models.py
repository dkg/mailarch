from email.utils import parseaddr
from collections import OrderedDict
import os
import re
import shutil
import subprocess

from django.db.models.signals import pre_delete, post_delete, post_save
from django.dispatch.dispatcher import receiver
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.db import models
from django.utils.log import getLogger

from mlarchive.archive.generator import Generator
from mlarchive.archive.thread import parse_message_ids

TXT2HTML = ['/usr/bin/mhonarc', '-single']
ATTACHMENT_PATTERN = r'<p><strong>Attachment:((?:.|\n)*?)</p>'
REFERENCE_RE = re.compile(r'<(.*?)>')

logger = getLogger('mlarchive.custom')


# --------------------------------------------------
# Helper Functions
# --------------------------------------------------

def get_in_reply_to_message(in_reply_to_value, email_list):
    '''Returns the in_reply_to message, if it exists'''
    msgids = parse_message_ids(in_reply_to_value)
    if not msgids:
        return None
    return get_message_prefer_list(msgids[0],email_list)


def get_message_prefer_list(msgid, email_list):
    '''Returns Message (or None) prefers proivded list'''
    try:
        return Message.objects.get(msgid=msgid, email_list=email_list)
    except Message.DoesNotExist:
        return Message.objects.filter(msgid=msgid).first()

# --------------------------------------------------
# Models
# --------------------------------------------------

class Thread(models.Model):
    first = models.ForeignKey(
        'Message',
        on_delete=models.SET_NULL,
        related_name='thread_key',
        blank=True,
        null=True)  # first message in thread, by date
    date = models.DateTimeField(db_index=True)     # date of first message

    def __unicode__(self):
        return str(self.id)

    def set_first(self, message=None):
        """Sets the first message of the thread.  Call when adding or removing
        messages
        """
        if not message:
            message = self.message_set.all().order_by('date').first()
        self.first = message
        self.date = message.date
        self.save()


class EmailList(models.Model):
    active = models.BooleanField(default=True, db_index=True)
    alias = models.CharField(max_length=255, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    description = models.CharField(max_length=255, blank=True)
    members = models.ManyToManyField(User)
    members_digest = models.CharField(max_length=28, blank=True)
    name = models.CharField(max_length=255, db_index=True, unique=True)
    private = models.BooleanField(default=False, db_index=True)
    updated = models.DateTimeField(auto_now=True)

    def __unicode__(self):
        return self.name

    @staticmethod
    def get_attachments_dir(listname):
        return os.path.join(settings.ARCHIVE_DIR, listname, '_attachments')

    @property
    def attachments_dir(self):
        return self.get_attachments_dir(self.name)

    @staticmethod
    def get_failed_dir(listname):
        return os.path.join(settings.ARCHIVE_DIR, listname, '_failed')

    @property
    def failed_dir(self):
        return self.get_failed_dir(self.name)

    @staticmethod
    def get_removed_dir(listname):
        return os.path.join(settings.ARCHIVE_DIR, listname, '_removed')

    @property
    def removed_dir(self):
        return self.get_removed_dir(self.name)


class Message(models.Model):
    base_subject = models.CharField(max_length=512, blank=True)
    cc = models.TextField(blank=True, default='')
    date = models.DateTimeField(db_index=True)
    email_list = models.ForeignKey(EmailList, db_index=True)
    frm = models.CharField(max_length=255, blank=True)
    from_line = models.CharField(max_length=255, blank=True)
    hashcode = models.CharField(max_length=28, db_index=True)
    in_reply_to = models.ForeignKey('self',null=True,related_name='replies')
    in_reply_to_value = models.TextField(blank=True, default='')
    # mapping to MHonArc message number
    legacy_number = models.IntegerField(blank=True, null=True, db_index=True)
    msgid = models.CharField(max_length=255, db_index=True)
    references = models.TextField(blank=True, default='')
    spam_score = models.IntegerField(default=0)             # > 0 = spam
    subject = models.CharField(max_length=512, blank=True)
    thread = models.ForeignKey(Thread)
    thread_depth = models.IntegerField(default=0)
    thread_order = models.IntegerField(default=0)
    to = models.TextField(blank=True, default='')
    updated = models.DateTimeField(auto_now=True)

    def __unicode__(self):
        return self.msgid

    def as_html(self):
        """Returns the message formated as HTML.  Uses MHonarc standalone
        Not used as of v1.00
        """
        with open(self.get_file_path()) as f:
            mhout = subprocess.check_output(TXT2HTML, stdin=f)

        # extract body
        within = False
        body = []
        for line in mhout.splitlines():
            if line == '<!--X-Body-of-Message-End-->':
                within = False
            if within:
                body.append(line)
            if line == '<!--X-Body-of-Message-->':
                within = True

        str = '\n'.join(body)

        # strip attachment links
        body = re.sub(ATTACHMENT_PATTERN, '', str)

        return body

    @property
    def friendly_frm(self):
        pass

    @property
    def frm_email(self):
        """This property is the email portion of the "From" header all lowercase
        (the realname is stripped).  It is used in faceting search results as
        well as display.
        """
        return parseaddr(self.frm)[1].lower()

    @property
    def frm_realname(self):
        """This property is the realname portion of the "From" header.
        """
        realname = parseaddr(self.frm)[0]
        if realname:
            return realname
        else:
            return self.frm_email

    def get_absolute_url(self):
        # strip padding, "=", to shorten URL
        return reverse('archive_detail', kwargs={
            'list_name': self.email_list.name,
            'id': self.hashcode.rstrip('=')})

    def get_attachment_path(self):
        path = self.email_list.attachments_dir
        if not os.path.exists(path):
            os.makedirs(path)
            os.chmod(path, 02777)
        return path

    def get_body(self):
        """Returns the contents of the message body, text only for use in indexing.
        ie. HTML is stripped.  This is called from the index template.
        """
        gen = Generator(self)
        return gen.as_text()

    def get_body_html(self, request=None):
        """Returns the contents of the message body with as HTML, for use in display
        """
        gen = Generator(self)
        return gen.as_html(request=request)

    def get_body_raw(self):
        """Returns the raw contents of the message file.
        NOTE: this will include encoded attachments
        """
        try:
            with open(self.get_file_path()) as f:
                return f.read()
        except IOError as error:
            msg = 'Error reading message file: %s' % self.get_file_path()
            logger.warning(msg)
            return msg

    def get_file_path(self):
        return os.path.join(
            settings.ARCHIVE_DIR,
            self.email_list.name,
            self.hashcode)

    def get_from_line(self):
        """Returns the "From " envelope header from the original mbox file if it
        exists or constructs one.  Useful when exporting in mbox format.
        NOTE: returns unicode, call to_str() before writing to file.
        """
        if self.from_line:
            return u'From {}'.format(self.from_line)
        elif self.frm_email:
            return u'From {} {}'.format(
                self.frm_email,
                self.date.strftime('%a %b %d %H:%M:%S %Y'))
        else:
            return u'From (none) {}'.format(
                self.date.strftime('%a %b %d %H:%M:%S %Y'))

    def get_references(self):
        """Returns list of message-ids from References header"""
        # remove all whitespace
        refs = ''.join(self.references.split())
        refs = REFERENCE_RE.findall(refs)
        # de-dupe
        results = []
        for ref in refs:
            if ref not in results:
                results.append(ref)
        return results

    def get_references_messages(self):
        """Returns list of messages from Rerefences header"""
        messages = []
        for msgid in self.get_references():
            message = get_message_prefer_list(msgid,self.email_list)
            if message:
                messages.append(message)
        return messages


    def get_removed_dir(self):
        return self.email_list.removed_dir

    def list_by_date_url(self):
        return reverse(
            'archive_search') + '?email_list={}&index={}'.format(
                self.email_list.name,
                self.hashcode.rstrip('='))

    def list_by_thread_url(self):
        return reverse(
            'archive_search') + '?email_list={}&gbt=1&index={}'.format(
                self.email_list.name,
                self.hashcode.rstrip('='))

    def mark(self, bit):
        """Mark this message using the bit provided, using field spam_score
        """
        self.spam_score = self.spam_score | bit
        self.save()

    def next_in_list(self):
        """Return the next message in the list, ordered by date ascending"""
        messages = Message.objects
        messages = messages.filter(email_list=self.email_list,
            date__gte=self.date)
        messages = messages.order_by('date','id')
        messages = messages.exclude(id=self.id)
        return messages.first()

    def next_in_thread(self):
        """Return the next message in thread"""
        messages = self.thread.message_set.filter(thread_order__gt=self.thread_order)
        messages = messages.order_by('thread_order')
        return messages.first()

    def previous_in_list(self):
        """Return the previous message in the list, ordered by date ascending"""
        messages = Message.objects
        messages = messages.filter(email_list=self.email_list,
            date__lte=self.date)
        messages = messages.order_by('date','id')
        messages = messages.exclude(id=self.id)
        return messages.last()

    def previous_in_thread(self):
        """Return the previous message in thread"""
        messages = self.thread.message_set.filter(thread_order__lt=self.thread_order)
        messages = messages.order_by('thread_order')
        return messages.last()

    @property
    def thread_date(self):
        """Returns the date of the first message in the associated thread.  Use for
        grouping by Thread
        """
        return self.thread.date

    @property
    def to_and_cc(self):
        """Returns 'To' and 'CC' fields combined, for use in indexing
        """
        if self.cc:
            return self.to + ' ' + self.cc
        else:
            return self.to


class Attachment(models.Model):
    # message if problem with attachment
    error = models.CharField(max_length=255, blank=True)
    description = models.CharField(max_length=255)
    filename = models.CharField(max_length=255)
    message = models.ForeignKey(Message)
    name = models.CharField(max_length=255)

    def __unicode__(self):
        return self.name

    def get_absolute_url(self):
        return os.path.join(
            reverse('archive'),
            'attach',
            self.message.email_list.name,
            self.filename)

    def get_file_path(self):
        return os.path.join(self.message.get_atttachment_path(), self.filename)


class Legacy(models.Model):
    email_list_id = models.CharField(max_length=40)
    msgid = models.CharField(max_length=255, db_index=True)
    number = models.IntegerField()

    def __unicode__(self):
        return '%s:%s' % (self.email_list_id, self.msgid)


# --------------------------------------------------
# Signal Handlers
# --------------------------------------------------


def _get_lists():
    """ Returns a dictionary with list names (mailboxes) as keys. The value at
        each key is a list of usernames with read acces to the mailing list.
        If the list of usernames is empty, then any user is allowed to read
        the mailing list. """

    result = OrderedDict()
    for mail_list in EmailList.objects.all().order_by('name'):
        result[mail_list.name] = mail_list.members.values_list(
            'username', flat=True)
    return result


def _get_lists_as_xml():
    """ Provides the results of get_lists as an xml document."""
    lines = []
    lines.append("<ms_config>")
    for elist, members in _get_lists().items():
        lines.append(
            "  <shared_root name='%s' path='/var/isode/ms/shared/%s'>" %
            (elist, elist))
        if members:
            lines.append("    <user name='anonymous' access='none'/>")
            for member in members:
                lines.append(
                    "    <user name='%s' access='read,write'/>" % member)
        else:
            lines.append("    <user name='anonymous' access='read'/>")
            lines.append("    <group name='anyone' access='read,write'/>")
        lines.append("  </shared_root>")
    lines.append("</ms_config>")
    return "\n".join(lines)


def _export_lists():
    """Produce XML dump of list memberships and call external program"""
    # Dump XML
    data = _get_lists_as_xml()
    path = os.path.join(settings.EXPORT_DIR, 'email_lists.xml')
    try:
        if not os.path.exists(settings.EXPORT_DIR):
            os.mkdir(settings.EXPORT_DIR)
        with open(path, 'w') as file:
            file.write(data)
            os.chmod(path, 0666)
    except Exception as error:
        logger.error('Error creating export file: {}'.format(error))
        return

    # Call external script
    if hasattr(settings, 'NOTIFY_LIST_CHANGE_COMMAND'):
        command = settings.NOTIFY_LIST_CHANGE_COMMAND
        try:
            subprocess.check_call([command, path])
        except (OSError, subprocess.CalledProcessError) as error:
            logger.error(
                'Error calling external command: {} ({})'.format(
                    command, error))


@receiver(pre_delete, sender=Message)
def _message_remove(sender, instance, **kwargs):
    """When messages are removed, via the admin page, we need to move the message
    archive file to the "_removed" directory
    """
    path = instance.get_file_path()
    if not os.path.exists(path):
        return
    target = instance.get_removed_dir()
    if not os.path.exists(target):
        os.mkdir(target)
        os.chmod(target, 02777)
    shutil.move(path, target)
    logger.info('message file moved: {} => {}'.format(path, target))

    # if message is first of many in thread, should reset thread.first before
    # deleting
    if (instance.thread.first == instance and
            instance.thread.message_set.count() > 1):
        next_in_thread = instance.thread.message_set.order_by('date')[1]
        instance.thread.set_first(next_in_thread)


@receiver(post_save, sender=Message)
def _message_save(sender, instance, **kwargs):
    """When messages are saved, udpate thread info
    """
    if instance.date < instance.thread.date:
        instance.thread.set_first(instance)


@receiver([post_save, post_delete], sender=EmailList)
def _clear_cache(sender, instance, **kwargs):
    """If EmailList object is saved or deleted remove the list_info cache entry
    """
    cache.delete('list_info')


@receiver(post_save, sender=EmailList)
def _list_save_handler(sender, instance, **kwargs):
    _export_lists()
