# Copyright (c) 2017, Lenovo.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
ML2 Mechanism Driver for Lenovo NOS platforms.
"""
from oslo_config import cfg
from oslo_log import log as logging

from neutron_lib import constants as n_const
from neutron_lib.api.definitions import portbindings
from neutron_lib import constants as p_const
from neutron_lib.plugins.ml2 import api

from networking_lenovo.ml2 import config as conf
from networking_lenovo.ml2 import exceptions as excep
from networking_lenovo.ml2 import nos_db_v2 as nxos_db
from networking_lenovo.ml2 import nos_network_driver
from networking_lenovo.ml2 import constants as const
from networking_lenovo.ml2.switch import *

LOG = logging.getLogger(__name__)


class LenovoNOSMechanismDriver(api.MechanismDriver):

    """Lenovo NOS ML2 Mechanism Driver."""

    def initialize(self):
        # Create ML2 device dictionary from ml2_conf.ini entries.
        conf.ML2MechLenovoConfig()

        # Extract configuration parameters from the configuration file.
        self._nos_switches = conf.ML2MechLenovoConfig.nos_dict
        self._nos_common = conf.ML2MechLenovoConfig.nos_common
        self._nos_vtep = conf.ML2MechLenovoConfig.nos_vtep

        LOG.debug(_("nos_switches found = %s"), self._nos_switches)
        LOG.debug(_("nos_common = %s"), self._nos_common)
        LOG.debug(_("nos_vtep = %s"), self._nos_vtep)

        self._switchs = {}
        for k,v in self._nos_vtep.items():#k:[mgmt_ip, key] v:tunnel ip
            network_mode = NetworkMode.vlan
            sw = Switch(k[0],network_mode=network_mode)
            self._switchs.update({k:sw})

        self.driver = nos_network_driver.LenovoNOSDriver()

    def _valid_network_segment(self, segment):
        return (cfg.CONF.ml2_lenovo.managed_physical_network is None or
                cfg.CONF.ml2_lenovo.managed_physical_network ==
                segment[api.PHYSICAL_NETWORK])

    def _get_vlanid(self, segment):
        if (segment and segment[api.NETWORK_TYPE] == p_const.TYPE_VLAN and
            self._valid_network_segment(segment)):
            return segment.get(api.SEGMENTATION_ID)

    def _get_vxlanid(self, vlan_id):
        try:
            LOG.debug(_("NOS: vxlan_id = %s"), str(int(self._nos_common['vxlan_range_base']) + vlan_id))
            return int(self._nos_common['vxlan_range_base']) + vlan_id
        except:
            LOG.debug(_("NOS: Error in _get_vxlanid"))
            return 0

    def _is_deviceowner_compute(self, port):
        return port['device_owner'].startswith('compute')

    def _is_status_active(self, port):
        return port['status'] == n_const.PORT_STATUS_ACTIVE

    def _get_switch_info(self, host_id):
        host_connections = []
        for switch_ip, attr in self._nos_switches:
            network_mode = [const.VLAN]#this is the default network mode
            if str(attr) == str(host_id):
                if self._nos_switches.has_key((switch_ip, const.NETWORK_MODE)):#the network_mode configuration will apply to all the ports of a switch.
                    if const.VXLAN in self._nos_switches[switch_ip, const.NETWORK_MODE].split(','):
                        network_mode.append(const.VXLAN)
                for port_id in (
                    self._nos_switches[switch_ip, attr].split(',')):
                    if ':' in port_id:
                        intf_type, port = port_id.split(':')
                    else:
#                        intf_type, port = 'ethernet', port_id
                        intf_type, port = 'port', port_id
                    host_connections.append((switch_ip, intf_type, port, network_mode))

        if not host_connections:
            LOG.warning("No switch entry found for host %s" % host_id)

        return host_connections

    def _configure_nxos_db(self, vlan_id, device_id, host_id):
        """Create the nos database entry.

        Called during update precommit port event.
        """
        host_connections = self._get_switch_info(host_id)
        for switch_ip, intf_type, nos_port, network_mode in host_connections:
            if const.VXLAN in network_mode:
                vxlan_id = self._get_vxlanid(vlan_id)
            else:
                vxlan_id = 0
            port_id = '%s:%s' % (intf_type, nos_port)
            nxos_db.add_nosport_binding(port_id, str(vlan_id), switch_ip,
                                          device_id, vxlan_id)

    def _get_all_other_vtep_ip(self, virtual_interface_ip):
        vtep_list = []
        for k,v in self._nos_vtep.items():
            if v != virtual_interface_ip:
                vtep_list.append(v)
        return vtep_list

    def _configure_switch_entry(self, vlan_id, device_id, host_id):
        """Create a nos switch entry.

        if needed, create a VLAN in the appropriate switch/port and
        configure the appropriate interfaces for this VLAN.

        Called during update postcommit port event.
        """
        vlan_name = cfg.CONF.ml2_lenovo.vlan_name_prefix + str(vlan_id)
        host_connections = self._get_switch_info(host_id)

        # (nos_port,switch_ip) will be unique in each iteration.
        # But switch_ip will repeat if host has >1 connection to same switch.
        # So track which switch_ips already have vlan created in this loop.
        vlan_already_created = []
        port_vxlan_already_enabled = []
        for switch_ip, intf_type, nos_port, network_mode in host_connections:

            # The VLAN needs to be created on the switch if no other
            # instance has been placed in this VLAN on a different host
            # attached to this switch.  Search the existing bindings in the
            # database.  If all the instance_id in the database match the
            # current device_id, then create the VLAN, but only once per
            # switch_ip.  Otherwise, just trunk.
            all_bindings = nxos_db.get_nosvlan_binding(vlan_id, switch_ip)
            previous_bindings = [row for row in all_bindings
                    if row.processed and (row.instance_id != device_id)]
            if previous_bindings or (switch_ip in vlan_already_created):
                LOG.debug("NOS: trunk vlan %s" % vlan_name)
                self.driver.enable_vlan_on_trunk_int(switch_ip, vlan_id,
                                                     intf_type, nos_port)
            else:
                vlan_already_created.append(switch_ip)
                LOG.debug("NOS: create & trunk vlan %s" % vlan_name)
                self.driver.create_and_trunk_vlan(
                    switch_ip, vlan_id, vlan_name, intf_type, nos_port)

            port_id = '%s:%s' % (intf_type, nos_port)

            if const.VXLAN in network_mode:
                vxlan_id = self._get_vxlanid(vlan_id)
                try:
                    virtuel_interface_ip = self._nos_vtep[switch_ip, 'virtuel_interface_ip']
                    vtep_list = self._get_all_other_vtep_ip(virtuel_interface_ip)
                    if not self._switchs[switch_ip, 'virtuel_interface_ip'].get_nwv_global_config():
                        self.driver.config_nwv_global(switch_ip)
                        self._switchs[switch_ip, 'virtuel_interface_ip'].set_nwv_global_config(True)
                        LOG.debug("VXLAN: set_nwv_global_config for the first time")
                    try:
                        all_vlan_vxlan_bindings = nxos_db.get_vlan_vxlan_switch_binding(vlan_id, vxlan_id, switch_ip)
                        previous_vlan_vxlan_bindings = [row for row in all_vlan_vxlan_bindings
                                                        if row.processed and (
                                                        row.instance_id != device_id) and vxlan_id != 0]
                        if len(previous_vlan_vxlan_bindings) == 0:
                            self.driver.config_nwv_vxlan(switch_ip, virtuel_interface_ip, vlan_id,
                                                         vxlan_id, vtep_list)
                            # self._switchs[switch_ip, 'virtuel_interface_ip'].configure(
                            #     "vlan " + str(vlan_id) + " virtual-network " + str(vxlan_id) + ";")
                            # for k, v in self._nos_vtep.items():
                            #     if switch_ip not in k:
                            #         self._switchs[switch_ip, 'virtuel_interface_ip'].configure(
                            #             "vtep " + v + " virtual-network " + str(vxlan_id) + ";")
                    except excep.NOSPortBindingNotFound:
                        self.driver.config_nwv_vxlan(switch_ip, virtuel_interface_ip, vlan_id,
                                                     vxlan_id, vtep_list)
                        # self._switchs[switch_ip, 'virtuel_interface_ip'].configure(
                        #     "vlan " + str(vlan_id) + " virtual-network " + str(vxlan_id) + ";")
                        # for k, v in self._nos_vtep.items():
                        #     if switch_ip not in k:
                        #         self._switchs[switch_ip, 'virtuel_interface_ip'].configure(
                        #             "vtep " + v + " virtual-network " + str(vxlan_id) + ";")
                    try:
                        all_port_bindings = nxos_db.get_port_switch_bindings(port_id, switch_ip)
                        previous_port_vxlan_enable = [binding for binding in all_port_bindings if
                                                      binding.processed and vxlan_id != 0]
                        if len(previous_port_vxlan_enable) == 0 and (nos_port not in port_vxlan_already_enabled):
                            LOG.debug(_("VXLAN:_configure_switch_entry previous_port_vxlan_enable is None, configure"))
                            port_vxlan_already_enabled.append(nos_port)
                            self.driver.enable_vxlan_on_int(switch_ip, intf_type, nos_port)
                            self._switchs[switch_ip, 'virtuel_interface_ip'].configure(
                                "interface " + intf_type + nos_port + ";vxlan enable;")
                    except excep.NOSPortBindingNotFound:
                        LOG.debug(_("VXLAN:_configure_switch_entry not found previous_port_vxlan_enable, configure"))
                        port_vxlan_already_enabled.append(nos_port)
                        self.driver.enable_vxlan_on_int(switch_ip, intf_type, nos_port)
                        self._switchs[switch_ip, 'virtuel_interface_ip'].configure(
                            "interface " + intf_type + nos_port + ";vxlan enable;")

                except KeyError:
                    LOG.debug("NOS: Configure error: switch ip %s, no virtuel_interface_ip for VXLAN mode"%switch_ip)

                nxos_db.process_binding(port_id, vlan_id, switch_ip, device_id)
            # self._switchs[(switch_ip, 'virtuel_interface_ip')].show_running("NOS:"+switch_ip+":_configure_switch_entry ")


    def _delete_nxos_db(self, vlan_id, device_id, host_id):
        """Delete the nos database entry.

        Called during delete precommit port event.
        """
        try:
            rows = nxos_db.get_nosvm_bindings(vlan_id, device_id)
            for row in rows:
                nxos_db.remove_nosport_binding(
                    row.port_id, row.vlan_id, row.switch_ip, row.instance_id, row.vxlan_id)
        except excep.NOSPortBindingNotFound:
            return

    def _delete_switch_entry(self, vlan_id, device_id, host_id):
        """Delete the nos switch entry.

        By accessing the current db entries determine if switch
        configuration can be removed.

        Called during update postcommit port event.
        """
        host_connections = self._get_switch_info(host_id)

        # (nos_port,switch_ip) will be unique in each iteration.
        # But switch_ip will repeat if host has >1 connection to same switch.
        # So track which switch_ips already have vlan removed in this loop.
        vlan_already_removed = []
        for switch_ip, intf_type, nos_port, network_mode in host_connections:

            # if there are no remaining db entries using this vlan on this
            # nos switch port then remove vlan from the switchport trunk.
            port_id = '%s:%s' % (intf_type, nos_port)
            if const.VXLAN in network_mode:
                vxlan_id = self._get_vxlanid(vlan_id)
                try:
                    virtuel_interface_ip = self._nos_vtep[switch_ip, 'virtuel_interface_ip']
                    vtep_list = self._get_all_other_vtep_ip(virtuel_interface_ip)
                    try:
                        nxos_db.get_vlan_vxlan_switch_binding(vlan_id, vxlan_id,
                                                             switch_ip)
                        LOG.debug(_("VXLAN:_delete_switch_entry  still found vlan vxlan bindings, no delete"))
                    except excep.NOSPortBindingNotFound:
                        LOG.debug(_("VXLAN:_delete_switch_entry  not found vlan vxlan bindings, delete"))
                        self.driver.deconfig_nwv_vxlan(switch_ip, virtuel_interface_ip, vlan_id,
                                                       vxlan_id, vtep_list)
                        self._switchs[switch_ip, 'virtuel_interface_ip'].deconfigure("vlan "+str(vlan_id)+" virtual-network "+str(vxlan_id)+";")
                        for k, v in self._nos_vtep.items():
                            if switch_ip not in k:
                                self._switchs[switch_ip, 'virtuel_interface_ip'].deconfigure(
                                    "vtep " + v + " virtual-network " + str(vxlan_id)+";")
                    try:#find any item with a valid vxlan
                        port_switch_bindings = nxos_db.get_port_switch_bindings(port_id, switch_ip)
                        #when there is no valid vxlan record, disable vxlan on this port.
                        # LOG.debug(_("VXLAN:vxlan_port_switch_bindings:%s, port_switch_bindings:%s"), len(vxlan_port_switch_bindings), len(port_switch_bindings))
                        if port_switch_bindings:
                            vxlan_port_switch_bindings = [row for row in port_switch_bindings
                                                          if row.vxlan_id != 0]
                            if len(vxlan_port_switch_bindings) == 0:
                                LOG.debug(_("VXLAN:_delete_switch_entry found port vxlan bindings, but len is 0"))
                                self.driver.disable_vxlan_on_int(switch_ip, intf_type, nos_port)
                        else:
                            self.driver.disable_vxlan_on_int(switch_ip, intf_type, nos_port)
                    except excep.NOSPortBindingNotFound:#impossible logic
                        LOG.debug(_("VXLAN:_delete_switch_entry  not found port vxlan bindings, disable vxlan on port"))
                        self.driver.disable_vxlan_on_int(switch_ip, intf_type, nos_port)
                except KeyError:
                    pass

            try:
                nxos_db.get_port_vlan_switch_binding(port_id, vlan_id,
                                                     switch_ip)
            except excep.NOSPortBindingNotFound:
                self.driver.disable_vlan_on_trunk_int(switch_ip, vlan_id,
                                                      intf_type, nos_port)

                # if there are no remaining db entries using this vlan on this
                # nos switch then remove the vlan.
                try:
                    nxos_db.get_nosvlan_binding(vlan_id, switch_ip)
                except excep.NOSPortBindingNotFound:

                    # Do not perform a second time on same switch
                    if switch_ip not in vlan_already_removed:
                        self.driver.delete_vlan(switch_ip, vlan_id)
                        vlan_already_removed.append(switch_ip)


            # self._switchs[(switch_ip, 'virtuel_interface_ip')].show_running("NOS:"+switch_ip+"_del_switch_entry ")
    def _is_vm_migration(self, context):
        if not context.top_bound_segment and context.original_top_bound_segment:
            return context.host != context.original_host

    def _port_action(self, port, segment, func):
        """Verify configuration and then process event."""
        device_id = port.get('device_id')
        host_id = port.get(portbindings.HOST_ID)
        vlan_id = self._get_vlanid(segment)

        if vlan_id and device_id and host_id:
            func(vlan_id, device_id, host_id)
        else:
            fields = "vlan_id " if not vlan_id else ""
            fields += "device_id " if not device_id else ""
            fields += "host_id" if not host_id else ""
            raise excep.NOSMissingRequiredFields(fields=fields)

    def update_port_precommit(self, context):
        """Update port pre-database transaction commit event."""

        # if VM migration is occurring then remove previous database entry
        # else process update event.
        if self._is_vm_migration(context):
            self._port_action(context.original,
                              context.original_top_bound_segment,
                              self._delete_nxos_db)
        else:
            if (self._is_deviceowner_compute(context.current) and
                self._is_status_active(context.current)):
                self._port_action(context.current,
                                  context.top_bound_segment,
                                  self._configure_nxos_db)

    def update_port_postcommit(self, context):
        """Update port non-database commit event."""

        # if VM migration is occurring then remove previous nos switch entry
        # else process update event.
        if self._is_vm_migration(context):
            self._port_action(context.original,
                              context.original_top_bound_segment,
                              self._delete_switch_entry)
        else:
            if (self._is_deviceowner_compute(context.current) and
                self._is_status_active(context.current)):
                self._port_action(context.current,
                                  context.top_bound_segment,
                                  self._configure_switch_entry)

    def delete_port_precommit(self, context):
        """Delete port pre-database commit event."""
        if self._is_deviceowner_compute(context.current):
            self._port_action(context.current,
                              context.top_bound_segment,
                              self._delete_nxos_db)

    def delete_port_postcommit(self, context):
        """Delete port non-database commit event."""
        if self._is_deviceowner_compute(context.current):
            self._port_action(context.current,
                              context.top_bound_segment,
                              self._delete_switch_entry)
