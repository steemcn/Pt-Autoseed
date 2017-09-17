# -*- coding: utf-8 -*-
# Copyright (c) 2017-2020 Rhilip <rhilipruan@gmail.com>
# Licensed under the GNU General Public License v3.0

import logging
import re
import time
from threading import Thread

from utils.constants import period_f
from utils.load.config import setting
from utils.load.submodules import tc, db
from utils.pattern import pattern_group

TIME_TORRENT_KEEP_MIN = 86400  # The download torrent keep time even no reseed and in stopped status.


class Controller(object):
    # List of Tracker String
    downloading_torrent_id_queue = []

    active_obj_list = []
    unactive_tracker_list = []

    last_id_check = 0

    def __init__(self):
        self._active()

        thread_args = [(self._online_check, setting.CYCLE_CHECK_RESEEDER_ONLINE),
                       (self._shut_unreseeder_db, setting.CYCLE_SHUT_UNRESEEDER_DB),
                       (self._del_torrent_with_db, setting.CYCLE_DEL_TORRENT_CHECK)]
        for args in thread_args:
            Thread(target=period_f, args=args, daemon=True).start()

        self.update_torrent_info_from_rpc_to_db(force_clean_check=True)

    # Add Reseeder
    def _active(self):
        """
        Active the reseeder objects and append it to self.active_reseeder_list.
        Each object should follow those step(s):
            1. Import the package
            2. Instantiation The object
            3. If The reseeder active successfully (after session check), Append this reseeder to List
            4. Else, Send it tracker url to another List

        :return: None
        """
        # Byrbt
        from extractors.byrbt import Byrbt
        autoseed_byrbt = Byrbt(**setting.site_byrbt)
        if autoseed_byrbt.status:
            self.active_obj_list.append(autoseed_byrbt)

        # NPUBits
        from extractors.npubits import NPUBits
        autoseed_npubits = NPUBits(**setting.site_npubits)
        if autoseed_npubits.status:
            self.active_obj_list.append(autoseed_npubits)

        # nwsuaf6
        from extractors.nwsuaf6 import MTPT
        autoseed_nwsuaf6 = MTPT(**setting.site_nwsuaf6)
        if autoseed_nwsuaf6.status:
            self.active_obj_list.append(autoseed_nwsuaf6)

        # TJUPT
        from extractors.tjupt import TJUPT
        autoseed_tjupt = TJUPT(**setting.site_tjupt)
        if autoseed_tjupt.status:
            self.active_obj_list.append(autoseed_tjupt)

        self.unactive_tracker_list = [i for i in db.col_seed_list[3:]
                                      if i not in [i.db_column for i in self.active_obj_list]]
        logging.info("The assign reseeder objects:{lis}".format(lis=self.active_obj_list))

    # Internal cycle function
    def _online_check(self):
        for i in self.active_obj_list:
            i.online_check()

    def _shut_unreseeder_db(self):
        for tracker in self.unactive_tracker_list:  # Set un_reseed column into -1
            db.exec(sql="UPDATE `seed_list` SET `{cow}` = -1 WHERE `{cow}` = 0 ".format(cow=tracker))

    @staticmethod
    def _del_torrent_with_db():
        """Delete torrent(both download and reseed) with data from transmission and database"""
        logging.debug("Begin torrent's status check.If reach condition you set,You will get a warning.")
        time_now = time.time()
        for cow in db.exec(sql="SELECT * FROM `seed_list`", r_dict=True, fetch_all=True):
            sid = cow.pop("id")
            s_title = cow.pop("title")
            err = 0
            reseed_list = []
            torrent_id_list = [tid for tracker, tid in cow.items() if tid > 0]
            for tid in torrent_id_list:
                try:  # Ensure torrent exist
                    reseed_list.append(tc.get_torrent(torrent_id=tid))
                except KeyError:  # Mark err when the torrent is not exist.
                    err += 1

            delete = False
            if err is 0:  # It means all torrents in this cow are exist,then check these torrent's status.
                reseed_stop_list = []
                for t in reseed_list:
                    if t.status == "stopped":  # Mark the stopped torrent
                        if int(time_now - t.addedDate) > TIME_TORRENT_KEEP_MIN:  # At least seed time
                            reseed_stop_list.append(t)
                    elif setting.pre_delete_judge(torrent=t):
                        tc.stop_torrent(t.id)
                        logging.warning(
                            "Reach Target you set,Torrent \"{name}\" now stop, "
                            "With Uploaded {si:.2f} MiB, Ratio {ro:.2f} , "
                            "Keep time {ho:.2f} h".format(name=t.name, si=t.uploadedEver / 1024 / 1024,
                                                          ro=t.uploadRatio,
                                                          ho=(time.time() - t.startDate) / 60 / 60)
                        )
                if len(reseed_list) == len(reseed_stop_list):
                    delete = True
                    logging.info("All torrents of \"{0}\" reach target,Will DELETE them soon.".format(s_title))
            else:
                delete = True
                logging.error("some Torrents (\"{name}\",{er} of {co}) may not found,"
                              "Delete all records from db".format(name=s_title, er=err, co=len(torrent_id_list)))

            if delete:  # Delete torrents with it's data and db-records
                for tid in torrent_id_list:
                    tc.remove_torrent(tid, delete_data=True)
                db.exec(sql="DELETE FROM `seed_list` WHERE `id` = {0}".format(sid))

    @staticmethod
    def _get_torrent_info(t) -> tuple:
        """
        Get torrent's information about tid, name and it's main tracker host.
        For main tracker host,if it is not in whole_tracker_list,will be rewrite to "download_id"

        :param t: int or class 'transmissionrpc.torrent.Torrent'
        :return: (tid, name, tracker)
        """
        if isinstance(t, int):
            t = tc.get_torrent(t)

        try:
            tracker = re.search(r"p[s]?://(?P<host>.+?)/", t.trackers[0]["announce"]).group("host")
            if tracker not in db.col_seed_list:
                raise AttributeError("Not reseed tracker.")
        except AttributeError:
            tracker = "download_id"
        return t.id, t.name, tracker

    def reseeder_feed(self, dl_torrent):
        pre_reseeder_list = [s for s in self.active_obj_list if s.suspended == 0]  # Get active and online reseeder

        tname = dl_torrent.name
        cow = db.exec("SELECT * FROM `seed_list` WHERE `download_id`='{did}'".format(did=dl_torrent.id), r_dict=True)

        reseed_status = False
        for pat in pattern_group:
            search = re.search(pat, tname)
            if search:
                logging.debug("The search group: {gr}".format(gr=search.groups()))
                key_raw = re.sub(r"[_\-.']", " ", search.group("search_name"))
                clone_dict = db.get_data_clone_id(key=key_raw)
                for reseeder in pre_reseeder_list:  # Site feed
                    if int(cow[reseeder.db_column]) is 0:
                        try:
                            tag = reseeder.torrent_feed(torrent=dl_torrent, name_pattern=search,
                                                        clone_db_dict=clone_dict)
                        except OSError as e:
                            logging.critical(e.args)
                            self._online_check()
                            pass
                        else:
                            db.reseed_update(did=dl_torrent.id, rid=tag, site=reseeder.db_column)
                            # self.last_id_check = tag
                reseed_status = True
                break

        if not reseed_status:  # Update seed_id == -1 if no matched pattern
            logging.warning("No match pattern,Mark \"{}\" As Un-reseed torrent,Stop watching.".format(tname))
            for reseeder in pre_reseeder_list:
                db.reseed_update(did=dl_torrent.id, rid=-1, site=reseeder.db_column)

    def reseeders_update(self):
        """Get the pre-reseed list from database."""
        pre_reseeder_list = [i for i in self.active_obj_list if i.suspended == 0]
        pre_cond = " OR ".join(["`{}`=0".format(i.db_column) for i in pre_reseeder_list])
        result = db.exec("SELECT * FROM `seed_list` WHERE `download_id` != 0 AND ({})".format(pre_cond),
                         r_dict=True, fetch_all=True)
        for t in result:  # Traversal all un-reseed list
            try:
                dl_torrent = tc.get_torrent(t["download_id"])
            except KeyError:  # Un-exist pre-reseed torrent
                logging.error("The pre-reseed Torrent (which name: \"{0}\") isn't found in result,"
                              "It will be deleted from db in next delete-check time".format(t["title"]))
            else:
                tname = dl_torrent.name
                if int(dl_torrent.progress) is 100:  # Get the download progress in percent.
                    logging.info("New completed torrent: \"{name}\" ,Judge reseed or not.".format(name=tname))
                    self.reseeder_feed(dl_torrent=dl_torrent)
                    if dl_torrent.id in self.downloading_torrent_id_queue:
                        self.downloading_torrent_id_queue.remove(dl_torrent.id)
                elif dl_torrent.id in self.downloading_torrent_id_queue:
                    pass  # Wait until this torrent download completely.
                else:
                    logging.warning("Torrent:\"{name}\" is still downloading,Wait......".format(name=tname))
                    self.downloading_torrent_id_queue.append(dl_torrent.id)

    def update_torrent_info_from_rpc_to_db(self, last_id_db=None, force_clean_check=False):
        """
        Sync torrent's id from transmission to database,
        List Start on last check id,and will return the max id as the last check id.
        """
        torrent_list = tc.get_torrents()  # Cache the torrent list
        new_torrent_list = [t for t in torrent_list if t.id > self.last_id_check]
        if new_torrent_list:
            last_id_now = max([t.id for t in new_torrent_list])
            if last_id_db is None:
                col_dl_reseeder = db.col_seed_list[2:]
                last_id_db = db.get_max_in_seed_list(column_list=col_dl_reseeder)
            logging.debug("Max tid, transmission: {tr},database: {db}".format(tr=last_id_now, db=last_id_db))

            if not force_clean_check:  # Normal Update
                logging.info("Some new torrents were add to transmission,Sync to db~")
                for i in new_torrent_list:  # Upsert the new torrent
                    db.upsert_seed_list(self._get_torrent_info(i))

            elif last_id_now != last_id_db:  # Check the torrent 's record between tr and db
                total_num_in_tr = len(set([t.name for t in torrent_list]))
                total_num_in_db = db.exec(sql="SELECT COUNT(*) FROM `seed_list`")[0]
                if int(total_num_in_tr) >= int(total_num_in_db):
                    db.cache_torrent_list()
                    logging.info("Upsert the whole torrent id to database.")
                    for t in torrent_list:  # Upsert the whole torrent
                        db.upsert_seed_list(self._get_torrent_info(t))

                else:  # TODO check....
                    logging.error("The torrent list didn't match with db-records,Clean the \"seed_list\" for safety.")
                    db.exec(sql="DELETE FROM `seed_list` WHERE 1")  # Delete all line from seed_list
                    self.update_torrent_info_from_rpc_to_db(last_id_db=0)
            self.last_id_check = last_id_now
        else:
            logging.debug("No new torrent(s),Return with nothing to do.")
        return self.last_id_check
