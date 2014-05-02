#!/usr/bin/python
'''
This is the Mailman External Archive interface for the Email Archive.  It takes an email message
on standard input and saves the message in the archive.  The message listname is required as the
first argument.  Use --public to specifiy a public list or --private to specify a private list.
The default is public.
'''
# Set PYTHONPATH and load environment variables for standalone script -----------------
# for file living in project/bin/
import os
import sys
path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not path in sys.path:
    sys.path.insert(0, path)
os.environ['DJANGO_SETTINGS_MODULE'] = 'mlarchive.settings.production'
# -------------------------------------------------------------------------------------

from optparse import OptionParser

import mlarchive.archive.management.commands._classes as _classes

from django.utils.log import getLogger
logger = getLogger('archive-mail')

def main():
    logger.info('called with arguments: %s' % sys.argv)
    usage = "usage: %prog LISTNAME [options]"
    parser = OptionParser(usage=usage)
    parser.add_option("--public", help="archive message to public archive (default)",
                      action="store_true", dest='public', default=False)
    parser.add_option("--private", help="archive message to private archive",
                      action="store_true", dest='private', default=False)
    (options, args) = parser.parse_args()

    # TODO should we use EX_TEMPFAIL?
    try:
        listname = sys.argv[1]
    except IndexError:
        sys.exit("%s: missing listname\nTry `%s --help for more information" %
                 (sys.argv[0],sys.argv[0]))

    if options.public and options.private:
        sys.exit("%s: invalid arguments\nTry `%s --help for more information" %
                 (sys.argv[0],sys.argv[0]))

    data = sys.stdin.read()
    logger.info('envelope: %s' % data.split('\n', 1)[0])
    status = _classes.archive_message(data,listname,private=options.private)

    #sys.exit(status)
    # NOTE: unless it is determined that mailman retries work correctly return 0
    logger.info('archive_message exit status: %s' % status)
    sys.exit(0)

if __name__ == "__main__":
    main()
