"""
Derived class from uploader which manages uploading to Google Drive account.
"""
import os
import shutil
from datetime import datetime
from logging import getLogger

from oauth2client import client, file
from oauth2client.clientsecrets import InvalidClientSecretsError
from googleapiclient.http import HttpError, MediaFileUpload, UnexpectedMethodError, \
    ResumableUploadError, UnexpectedBodyError
from googleapiclient import discovery
from httplib2 import Http

from .uploader import Uploader
from .. import __nephos_dir__, __log_dir__
from ..exceptions import OAuthFailure, UploadingFailed
from ..manage_db import DBHandler, TSK_STORE_INDEX, TSK_SHR_INDEX
from ..mail_notifier import send_mail, add_to_report


LOG = getLogger(__name__)
SCOPES = "https://www.googleapis.com/auth/drive"
APPLICATION_NAME = "Project Nephos"
CRED_PATH = os.path.join(__nephos_dir__, ".up_cred")
CLI_SECRET_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), ".client_secrets")
LOG_DRIVE_FOLDER_ID = "1M8jl0tDPoN3K6TE6KipwJZYXsMxb75Do"
LOG_FILE_PATH = os.path.join(__log_dir__, "nephos.log")


class GDrive(Uploader):
    """
    Derived from uploader and handles uploading recordings
    to Google drive.
    """
    def auth(self):
        """
        Runs authentication pipeline.

        Returns
        -------

        """
        store = file.Storage(CRED_PATH)
        try:
            credentials = self._auth_from_file(store)
            LOG.info("Drive API authenticated using saved credentials!")
        except OAuthFailure:
            credentials = self._init_auth_flow()

        store.put(credentials)

        try:
            http = credentials.authorize(Http())
            self.service = discovery.build("drive", "v3", http=http, cache_discovery=False)
        except HttpError as error:
            LOG.critical("Authentication request failed!")
            LOG.debug(error)
            send_mail("Please re-authenticate Nephos, authentication attempt failed with "
                      "error:\n{error}\n".format(
                          error=error
                          ), "critical")
            raise OAuthFailure

    @staticmethod
    def _get_upload_service():
        """
        Returns
        -------
        file_upload_service
            type: googleapiclient.discovery.build
            service to handle uploading and adding permissions for user
        """
        http = GDrive._auth_from_file(file.Storage(CRED_PATH)).authorize(Http())
        return discovery.build("drive", "v3", http=http, cache_discovery=False)

    @staticmethod
    def _upload(tasks_list):
        """
        Uploads the folder and appends share entities

        Parameters
        -------
        tasks_list
            type:  list
            list containing details of recordings to be uploaded.

        Returns
        -------

        """
        service = GDrive._get_upload_service()
        file_service = service.files()  # pylint: disable=no-member
        permissions_service = service.permissions()  # pylint: disable=no-member
        batch_service = service.new_batch_http_request(callback=GDrive._share_callback)  # pylint: disable=no-member
        for task in tasks_list:
            folder, share_list = task[TSK_STORE_INDEX], task[TSK_SHR_INDEX]
            folder_id, error = None, None
            try:
                try:
                    GDrive._set_uploading(folder)
                    folder_id = GDrive._create_folder(file_service, folder)
                    GDrive._upload_files(file_service, folder, folder_id)
                    GDrive._share(batch_service, permissions_service, folder_id, share_list)
                    LOG.debug("%s uploaded successfully!", folder)
                    GDrive._remove(folder)
                    LOG.debug("%s removed from local storage successfully!", folder)
                except (UnexpectedBodyError, ResumableUploadError, UnexpectedMethodError,
                        HttpError) as err:
                    LOG.warning("Uploading %s failed! Will retry later", folder)
                    LOG.debug(err)
                    with DBHandler.connect() as db_cur:
                        folder_id, error = None, err
                        raise UploadingFailed(folder, db_cur)
            except UploadingFailed:
                pass

            if folder_id is not None:
                add_to_report("{folder} successfully uploaded to drive (folderid = {folder_id}), "
                              "and shared with {share_lists}.".format(
                                  folder=task[TSK_STORE_INDEX],
                                  folder_id=folder_id,
                                  share_lists=task[TSK_SHR_INDEX]
                                  ))
            else:
                add_to_report("{folder} uploading to drive failed due to "
                              "following error\n{error}\n".format(
                                  folder=task[TSK_STORE_INDEX],
                                  error=error
                                  ))

        # uploading logs with every upload.
        GDrive.upload_log(file_service)

    @staticmethod
    def upload_log(file_service):
        """
        Uploads the log file and truncates the previous one to have logs only
        reports beyond this point.

        Parameters
        -------
        file_service
            file managing service for google drive


        Returns
        -------

        """
        timestamped_log_path = os.path.join(__log_dir__,
                                            str(datetime.now().strftime(
                                                "nephos_%d%m%Y_%H%M.log")))
        shutil.copyfile(LOG_FILE_PATH, timestamped_log_path)
        file_id = GDrive._upload_file(file_service, LOG_DRIVE_FOLDER_ID, timestamped_log_path)
        if file_id is not None:
            open(LOG_FILE_PATH, 'w').close()
            LOG.debug("Logs till here uploaded to 'Nephos_Logs' "
                      "drive folder (file id: %s).", file_id)
        else:
            LOG.warning("Uploading logs to drive (folder id: %s) failed!", LOG_DRIVE_FOLDER_ID)
        os.remove(timestamped_log_path)

    @staticmethod
    def _auth_from_file(store):
        """
        Uses the stored credentials to authenticate.

        Parameters
        -------
        store
            type: oauth2client.file.Storage
            link to the file containing credentials

        Returns
        -------
        credentials
            type: Oauth2Credentials
            Auth credentials stored in the file.
        """
        credentials = store.get()

        def raise_error():
            LOG.warning("Authentication using credentials file failed!")
            raise OAuthFailure

        if not credentials:
            raise_error()
        elif credentials.invalid:
            raise_error()

        return credentials

    @staticmethod
    def _init_auth_flow():
        """
        Authenticates with the google account via OAuth2.

        Runs at the first start of Nephos, and stores the authentication in a file. If the
        file is somehow tampered, this authentication method will be called again.

        Returns
        -------
        credentials
            type: OAuth2Credentials
            The credentials after the flow is successful.
        """

        try:
            flow = client.flow_from_clientsecrets(
                filename=CLI_SECRET_PATH,
                scope=SCOPES,
                redirect_uri="urn:ietf:wg:oauth:2.0:oob"  # GUI is not opened
            )
        except (InvalidClientSecretsError, ValueError) as error:
            LOG.error("Invalid client secrets file provided at %s",
                      CLI_SECRET_PATH
                      )
            LOG.debug(error)
            raise OAuthFailure()

        flow.user_agent = APPLICATION_NAME
        url = flow.step1_get_authorize_url()

        LOG.critical("Please visit the following URL: %s",
                     url
                     )
        code = input("Enter the code: ")

        try:
            credentials = flow.step2_exchange(code)
        except client.FlowExchangeError as error:
            LOG.error("Failed to authenticate!")
            LOG.debug(error)
            raise OAuthFailure()
        LOG.info("Authenticated successfully")

        return credentials

    @staticmethod
    def _create_folder(file_service, folder):
        """
        Creates the folder to upload the recording to.

        Parameters
        ----------
        file_service
            file managing service for google drive
        folder
            type: str
            folder to the uploaded

        Returns
        -------

        """
        file_metadata = {
            'name': GDrive._get_name(folder),
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder_id = file_service.create(
            body=file_metadata,
            fields='id'
        ).execute().get('id')
        LOG.debug("%s folder created, id: %s", folder, folder_id)
        return folder_id

    @staticmethod
    def _upload_files(file_service, folder, folder_id):
        """
        uploads files present in the folder to google drive under
        the provided folder's id.

        Parameters
        ----------
        file_service
            file managing service for google drive
        folder
            type: str
            absolute path of folder to be uploaded
        folder_id
            type: str
            unique folder id of the cloud parent folder

        Returns
        -------

        """
        files = [os.path.join(folder, x) for x in os.listdir(folder)]
        try:
            files.remove(os.path.join(folder, 'ffmpeg2pass-0.log.mbtree'))
        except ValueError:
            pass
        for file_path in files:
            GDrive._upload_file(file_service, folder_id, file_path)

    @staticmethod
    def _upload_file(file_service, folder_id, file_path):
        """
        uploads a single file to the drive

        Parameters
        -------
        file_service
            file managing service for google drive
        folder_id
            type: str
            unique folder id of the cloud parent folder
        file_path
            type: str
            absolute path of the file to be uploaded

        Returns
        -------

        """
        file_metadata = {
            'name': GDrive._get_name(file_path),
            'parents': [folder_id]
        }
        media = MediaFileUpload(
            file_path,
            mimetype=GDrive._get_mimetype(GDrive._get_name(file_path)),
            resumable=True
        )
        file_id = file_service.create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute().get('id')
        LOG.debug("%s uploaded to file ID: %s", file_path, file_id)
        return file_id

    @staticmethod
    def _share(batch_service, perm_service, folder_id, share_list):
        """
        Share a given folderid with some other entity.
        Role defines the level of access the entity can have.
        The supported values for role are: owner, reader, writer or commenter.

        Currently we add everyone as reader.

        Parameters
        ----------
        batch_service
            batch http requests managing service for google drive
        perm_service
            permissions managing service for google drive
        folder_id
            type: str
            unique folder id of the cloud folder
        share_list
            type: str
            str of entities the file is to be shared with,
            multiple values separated by space

        Returns
        -------

        """
        for email in share_list.split():
            permission = {
                "type": "user",
                "role": "reader",
                "emailAddress": email
            }
            batch_service.add(perm_service.create(
                fileId=folder_id,
                body=permission,
                fields='id',
            ))

        batch_service.execute()

    @staticmethod
    def _get_mimetype(filename):
        """
        https://developers.google.com/drive/api/v3/mime-types

        Parameters
        ----------
        filename
            type: str
            name of the file

        Returns
        -------
        mimeType
            type: str
            google drive mimetype

        """
        name_parts = filename.split('.')
        extension = name_parts[len(name_parts)-1]

        mimetype = {
            "mp4": 'video/mp4',
            "txt": 'text/plain',
            "srt": 'text/plain',
            "log": 'text/plain'
        }

        return mimetype[extension]

    @staticmethod
    def _share_callback(_, response, exception):
        if exception:
            # Handle error
            LOG.debug(exception)
        else:
            LOG.debug("Permission Id: %s", response.get('id'))
