<?php

/*
 * Copyright (C) 2022 Deciso B.V.
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 *
 * 1. Redistributions of source code must retain the above copyright notice,
 *    this list of conditions and the following disclaimer.
 *
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED ``AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES,
 * INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
 * AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
 * AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY,
 * OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
 * SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 * INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
 * CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
 * ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 * POSSIBILITY OF SUCH DAMAGE.
 */

namespace OPNsense\Interfaces\Api;

use OPNsense\Core\Backend;
use OPNsense\Core\Config;
use OPNsense\Base\UserException;
use OPNsense\Base\ApiMutableModelControllerBase;

class VlanSettingsController extends ApiMutableModelControllerBase
{
    protected static $internalModelName = 'vlan';
    protected static $internalModelClass = 'OPNsense\Interfaces\Vlan';

    private function generateVlanIfName()
    {
        $tmp = $this->request->getPost("vlan");
        return "{$tmp['if']}_vlan{$tmp['tag']}";
    }

    private function interfaceAssigned($if)
    {
        $configHandle = Config::getInstance()->object();
        if (!empty($configHandle->interfaces)) {
            foreach ($configHandle->interfaces->children() as $ifname => $node) {
                if ((string)$node->if == $if) {
                    return true;
                }
            }
        }
        return false;
    }

    public function searchItemAction()
    {
        return $this->searchBase("vlan", ['vlanif','if','tag','pcp','descr'], "vlanif");
    }

    public function setItemAction($uuid)
    {
        $node = $this->getModel()->getNodeByReference('vlan.' . $uuid);
        $old_vlanif = $node != null ? (string)$node->vlanif : null;
        $new_vlanif = $this->generateVlanIfName();
        if ($old_vlanif != null && $old_vlanif != $new_vlanif && $this->interfaceAssigned($old_vlanif)) {
            $tmp = $this->request->getPost("vlan");
            if ($tmp['tag'] != (string)$node->tag) {
                $result = [
                  "result" => "failed",
                  "validations" => [
                      "vlan.tag" => gettext("Interface is assigned and you cannot change the VLAN tag while assigned.")
                  ]
                ];
            } else {
                $result = [
                  "result" => "failed",
                  "validations" => [
                      "vlan.if" => gettext("Interface is assigned and you cannot change the parent while assigned.")
                  ]
                ];
            }
        } else {
            $result = $this->setBase("vlan", "vlan", $uuid, ["vlanif" => $new_vlanif]);
            // store interface name for apply action
            if ($result['result'] != 'failed' && $old_vlanif != $new_vlanif) {
                file_put_contents("/tmp/.vlans.removed", "{$old_vlanif}\n", FILE_APPEND|LOCK_EX);
            }
        }
        return $result;
    }

    public function addItemAction()
    {
        return $this->addBase("vlan", "vlan", [
            "vlanif" =>  $this->generateVlanIfName()
        ]);
    }

    public function getItemAction($uuid = null)
    {
        return $this->getBase("vlan", "vlan", $uuid);
    }

    public function delItemAction($uuid)
    {
        $node = $this->getModel()->getNodeByReference('vlan.' . $uuid);
        $old_vlanif = $node != null ? (string)$node->vlanif : null;
        if ($old_vlanif != null && $this->interfaceAssigned($old_vlanif)) {
            throw new UserException(gettext("This VLAN cannot be deleted because it is assigned as an interface."));
        } else {
            $result = $this->delBase("vlan", $uuid);
            // store interface name for apply action
            if ($result['result'] != 'failed') {
                file_put_contents("/tmp/.vlans.removed", "{$old_vlanif}\n", FILE_APPEND|LOCK_EX);
            }
            return $result;
        }
    }

    public function reconfigureAction()
    {
        $result = array("status" => "failed");
        if ($this->request->isPost()) {
            $result['status'] = strtolower(trim((new Backend())->configdRun('interface vlan configure')));
        }
        return $result;
    }
}
