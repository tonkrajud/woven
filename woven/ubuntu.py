#!/usr/bin/env python
import os,sys

from fabric.api import env, run, sudo, get, put
from fabric.state import connections
from fabric.context_managers import settings
from fabric.operations import prompt
from fabric.contrib.files import comment, uncomment, contains, exists, append, sed
from fabric.contrib.console import confirm
from fabric.network import join_host_strings, normalize

from woven.utils import backup_file, restore_file
from woven.utils import server_state, set_server_state, upload_template
    
def add_user(username='',password='',group=''):
    """
    Adds the username
    """
    if group: group = '-g %s'% group
    
    run('echo %s:%s > /tmp/users.txt'% (username,password))
    sudo('useradd -m -s /bin/bash %s %s'% (group,username))
    sudo('chpasswd < /tmp/users.txt')
    sudo('rm -rf /tmp/users.txt')
    
def apt_get_install(package):
    """
    Wrapper around apt_get install
    """
    #install silent and answer yes by default -qqy
    sudo('apt-get install -qqy %s'% package, pty=True)

def apt_get_purge(package):
    """
    Wrapper around apt-get purge
    """
    sudo('apt-get purge -qqy %s'% package, pty=True)
    
def change_ssh_port(rollback=False):
    """
    This would be the first function to be run to setup a server.
    By changing the ssh port first we can test it to see if it has been
    changed, and thus can reasonably assume that root has also been disabled
    in a previous setupserver execution
    
    Returns success or failure
    """

    if not rollback:
        after = env.port
        before = str(env.DEFAULT_SSH_PORT)
    else:
        after = str(env.DEFAULT_SSH_PORT)
        before = env.port
    host = normalize(env.host_string)[1]
    host_string=join_host_strings('root',host,before)

    with settings(host_string=host_string, user='root', password=env.ROOT_PASSWORD):
        if env.verbosity:
            print env.host, "CHANGING SSH PORT TO: "+str(after)
        if not rollback:
                try:
                    #Both test the port and also the ubuntu version.
                    distribution, version = ubuntu_version()
                    #    print env.host, distribution, version
                    if version < 9.10:
                        print env.host, 'Woven is only compatible with Ubuntu versions 9.10 and greater'
                        sys.exit(1)
                except KeyboardInterrupt:
                    if env.verbosity:
                        print >> sys.stderr, "\nStopped."
                    sys.exit(1)
                except: #No way to catch the failing connection without catchall? 
                    print env.host, "Warning: Default port not responding. Setupnode may alredy have been run or the host is down. Skipping.."
                    return False
                sed('/etc/ssh/sshd_config','Port '+ str(before),'Port '+str(after),use_sudo=True)
                if env.verbosity:
                    print env.host, "RESTARTING SSH on",after
                sudo('/etc/init.d/ssh restart')
                set_server_state('ssh_port_changed',content=str(before))
                return True
        else:
            port = server_state('ssh_port_changed')
            if port:
                sed('/etc/ssh/sshd_config','Port '+ str(before),'Port '+str(after),use_sudo=True)
                set_server_state('ssh_port_changed',delete=True)
                sudo('/etc/init.d/ssh restart')
                return True
            return False
        

def disable_root(rollback=False):
    """
    Disables root and creates a new sudo user as specified by HOST_USER in your
    settings or your host_string
    
    The normal pattern for hosting is to get a root account which is then disabled.
    If root is disabled as it is in the default ubuntu install then set
    ROOT_DISABLED:True in your settings
    
    returns True on success
    """
    def enter_password():
        password1 = prompt('Enter the password for %s:'% original_username)
        password2 = prompt('Re-enter the password:')
        if password1 <> password2:
            print env.host, 'The password was not the same'
            enter_password()
        return password1
    if not rollback:
        #TODO write a test in paramiko to see whether root has already been disabled
        #Fabric doesn't have a way of detecting a login fail which would be the best way
        #that we could assume that root has been disabled
        #print env.host, 'settings:', env.host_string, env.user, env.port
        original_username = env.user
        original_password = env.get('HOST_PASSWORD','')
        (olduser,host,port) = normalize(env.host_string)
        host_string=join_host_strings('root',host,str(port))
        with settings(host_string=host_string,  password=env.ROOT_PASSWORD):
            if not contains('sudo','/etc/group',use_sudo=True):
                sudo('groupadd sudo')
                set_server_state('sudo-added')
            home_path = '/home/%s'% original_username
            if not exists(home_path, use_sudo=True):
                if env.verbosity:
                    print env.host, 'CREATING A NEW ACCOUNT: %s'% original_username
                
                if not original_password:

                    original_password = enter_password()
                
                add_user(username=original_username, password=original_password,group='sudo')
                
                #add user to /etc/sudoers
                if not exists('/etc/sudoers.wovenbak',use_sudo=True):
                    sudo('cp -f /etc/sudoers /etc/sudoers.wovenbak')
                sudo('cp -f /etc/sudoers /tmp/sudoers.tmp')
                append("# Members of the sudo group may gain root privileges", '/tmp/sudoers.tmp', use_sudo=True)
                append("%sudo ALL=(ALL) ALL", '/tmp/sudoers.tmp', use_sudo=True)
                sudo('visudo -c -f /tmp/sudoers.tmp')
                sudo('cp -f /tmp/sudoers.tmp /etc/sudoers')
                sudo('rm -rf /tmp/sudoers.tmp')
            #Add existing user to sudo group
            else:
                sudo('adduser %s sudo'% original_username)
        env.password = original_password
        #finally disable root
        if env.verbosity:
            print env.host, 'DISABLING ROOT'
        sudo("usermod -L %s"% 'root')
        return True
    else: #rollback to root
        if not env.ROOT_PASSWORD:
            env.ROOT_PASSWORD = enter_password()
        run('echo %s:%s > /tmp/root_user.txt'% ('root',env.ROOT_PASSWORD))
        sudo('chpasswd < /tmp/root_user.txt')
        sudo('rm -rf /tmp/root_user.txt')
        print "Closing connection %s"% env.host_string
        connections[env.host_string].close()
        original_username = env.user
        (olduser,host,port) = normalize(env.host_string)
        host_string=join_host_strings('root',host,str(env.port))
        with settings(host_string=host_string,  password=env.ROOT_PASSWORD):
            if env.INTERACTIVE:
                c_text = 'CAUTION: Woven will now delete the user %s and the home directory. \n'% original_username
                c_text = 'Please ensure you can login as root before continuing.\n'
                c_text += 'Do you wish to continue:'
                proceed = confirm(c_text,default=False)
            if not env.INTERACTIVE or proceed:
                sudo('deluser --remove-home '+original_username)
                if server_state('sudo-added'): #never true on default installation
                    sudo('groupdel sudo')
                    set_server_state('sudo-added',delete=True)

def ubuntu_version():
    """
    Get the version # of Ubuntu as a float
    """
    version = run('cat /etc/issue').split(' ')[:2]
    try:
        version[1] = float(version[1])
    except ValueError:
        pass
    return version[0],version[1]

   
def install_packages(rollback = False,overwrite=False):
    """
    Install a set of baseline packages on Ubuntu Server
    and configure where necessary
    
    overwrite will allow existing configurations to be overwritten
    """
    u = env.HOST_BASE_PACKAGES + env.HOST_EXTRA_PACKAGES
    if not rollback:
        if env.verbosity:
            print env.host, "INSTALLING & CONFIGURING HOST PACKAGES:"
            #print ','.join(u)
        #Remove apparmor - TODO we may enable this later
        sudo('/etc/init.d/apparmor stop')
        sudo('update-rc.d -f apparmor remove')
        #Get a list of installed packages
        p = run("dpkg -l | awk '/ii/ {print $2}'").split('\n')
    
        #The principle we will use is to only install configurations and packages
        #if they do not already exist (ie manually installed or other method)
        
        for package in u:
            if not package in p:
                preinstalled = False
                apt_get_install(package)
                sudo("echo '%s' >> /var/local/woven/packages_installed.txt"% package)
                if package == 'apache2':
                    sudo("a2dissite 000-default")
                elif package == 'nginx':
                    sudo('rm -f /etc/nginx/sites-enabled/default')
                if env.verbosity:
                    print ' * installed '+package
            else:
                preinstalled = True

            if package == 'apache2' and (overwrite or not preinstalled):
                if env.verbosity:
                    print "Uploading Apache2 template /etc/apache2/ports.conf"
                context = {'host_ip':env.host}
                upload_template('woven/apache2/ports.conf','/etc/apache2/ports.conf',context=context, use_sudo=True)
                #Turn keep alive off on apache
                sed('/etc/apache2/apache2.conf',before='KeepAlive On',after='KeepAlive Off',use_sudo=True)
                with settings(warn_only=True):
                    sudo("apache2ctl stop")
            elif package == 'nginx' and (overwrite or not preinstalled):
                if env.verbosity:
                    print "Uploading Nginx templates /etc/nginx/nginx.conf /etc/nginx/proxy.conf"
                upload_template('woven/nginx/nginx.conf','/etc/nginx/nginx.conf',use_sudo=True)
                #Upload a default proxy
                upload_template('woven/nginx/proxy.conf','/etc/nginx/proxy.conf',use_sudo=True)
                with settings(warn_only=True):
                    sudo("/etc/init.d/nginx stop")

        #Set unattended-updates configuration
        unattended_config = '/etc/apt/apt.conf.d/10periodic'
        if not exists(unattended_config, use_sudo=True):
            if env.verbosity:
                "Configuring unattended-updates /etc/apt/apt.conf.d/10periodic"
            sudo('touch '+unattended_config)
            #in theory append() should intelligently ignore lines if they already exist
            #in practice this doesn't work as expected for this particular list.
            #possibly some characters it is not matching correctly hence if the
            #file already exists we'll skip this
            append([
                'APT::Periodic::Update-Package-Lists "1";',
                'APT::Periodic::Download-Upgradeable-Packages "1";',
                'APT::Periodic::AutocleanInterval "7";',
                'APT::Periodic::Unattended-Upgrade "1";',
            ], filename=' /etc/apt/apt.conf.d/10periodic',use_sudo=True)
            set_server_state('unattended_config_created')
        
        #Install base python packages
        #We'll use easy_install at this stage since it doesn't download if the package
        #is current whereas pip always downloads.
        #Once both these packages mature we'll move to using the standard Ubuntu packages

        sudo("easy_install -U virtualenv")
        sudo("easy_install -U pip")

    
        #cleanup after easy_install
        sudo("rm -rf build")
    else: #rollback
        p = sudo('cat /var/local/woven/packages_installed.txt').split('\n')
        for package in u:
            if package in p:
                apt_get_purge(package)
                p.remove(package)
    
        #Finally write back the list of packages
        sudo('rm -f /var/local/woven/packages_installed.txt')
        for package in p:
            sudo("echo '%s' >> /var/local/woven/packages_installed.txt"% package)
        
        #Rollback unattended updates
        if server_state('unattended_config_created'):
            sudo('rm -rf /etc/apt/apt.conf.d/10periodic')
            set_server_state('unattended_config_created',delete=True)
        #Finally remove any unneeded packages
        sudo('apt-get autoremove -qqy')


def restrict_ssh(rollback=False):
    """
    Set some sensible restrictions in Ubuntu /etc/ssh/sshd_config and restart sshd
    UseDNS no #prevents dns spoofing sshd defaults to yes
    X11Forwarding no # defaults to no
    AuthorizedKeysFile  %h/.ssh/authorized_keys

    uncomments PasswordAuthentication no and restarts sshd
    """

    if not rollback:
        if server_state('ssh_restricted'):
            print env.host, 'Warning: sshd_config has already been modified. Skipping..'
            return False

        sshd_config = '/etc/ssh/sshd_config'
        if env.verbosity:
            print env.host, "RESTRICTING SSH with "+sshd_config
        filename = 'sshd_config'
        if not exists('/home/%s/.ssh/authorized_keys'% env.user): #do not pass go do not collect $200
            print env.host, 'You need to upload_ssh_key first.'
            return False
        backup_file(sshd_config)
        context = {"HOST_SSH_PORT": env.HOST_SSH_PORT}
        
        upload_template('woven/ssh/sshd_config','/etc/ssh/sshd_config',context=context,use_sudo=True)
        # Restart sshd
        sudo('/etc/init.d/ssh restart')
        
        # The user can modify the sshd_config file directly but we save
        if env.INTERACTIVE and contains('#PasswordAuthentication no','/etc/ssh/sshd_config',use_sudo=True):
            c_text = 'Woven will now remove password login from ssh, and use only your ssh key. \n'
            c_text = c_text + 'CAUTION: please confirm that you can ssh %s@%s -p%s from a terminal without requiring a password before continuing.\n'% (env.user, env.host, env.port)
            c_text += 'If you cannot login, press enter to rollback your sshd_config file'
            proceed = confirm(c_text,default=False)
    
        if not env.INTERACTIVE or proceed:
            #uncomments PasswordAuthentication no and restarts
            uncomment(sshd_config,'#(\s?)PasswordAuthentication(\s*)no',use_sudo=True)
            sudo('/etc/init.d/ssh restart')
        else: #rollback
            print env.host, 'Rolling back sshd_config to default and proceeding without passwordless login'
            restore_file('/etc/ssh/sshd_config', delete_backup=False)
            sed('/etc/ssh/sshd_config','Port '+ str(env.DEFAULT_SSH_PORT),'Port '+str(env.HOST_SSH_PORT),use_sudo=True)
            
            sudo('/etc/init.d/ssh restart')
            return False
        set_server_state('ssh_restricted')
        return True
    else: #Full rollback
        restore_file('/etc/ssh/sshd_config')
        if server_state('ssh_port_changed'):
            sed('/etc/ssh/sshd_config','Port '+ str(env.DEFAULT_SSH_PORT),'Port '+str(env.HOST_SSH_PORT),use_sudo=True)
            sudo('/etc/init.d/ssh restart')
        sudo('/etc/init.d/ssh restart')
        set_server_state('ssh_restricted', delete=True)
        return True

def set_timezone(rollback=False):
    """
    Set the time zone on the server using Django settings.TIME_ZONE
    """
    if not rollback:
        if contains(text=env.TIME_ZONE,filename='/etc/timezone',use_sudo=True):
            if env.verbosity:
                print env.host, 'Time Zone already set to '+env.TIME_ZONE
            return False
        if env.verbosity:
            print env.host, "CHANGING TIMEZONE /etc/timezone to "+env.TIME_ZONE
        backup_file('/etc/timezone')
        sudo('echo %s > /tmp/timezone'% env.TIME_ZONE)
        sudo('cp -f /tmp/timezone /etc/timezone')
        sudo('dpkg-reconfigure --frontend noninteractive tzdata')
    else:
        restore_file('/etc/timezone')
        sudo('dpkg-reconfigure --frontend noninteractive tzdata')
    return True
    

def setup_ufw(rollback=False):
    """
    Setup ufw and apply rules from settings UFW_RULES
    You can add rules and re-run setup_ufw but cannot delete rules or reset by script
    since deleting or reseting requires user interaction
    
    See Ubuntu Server documentation for more about UFW.
    """
    if not rollback:
        #TODO - Optimize to store & compare existing rules to stop unecessary reloads
        #Should be able to do something with the ufw status command to store the rules
        #ufw_rules = sudo("ufw status | awk '/tcp|udp/ {print $1,$2,$3}'").split('\n')
        ufw = run("dpkg -l | grep '%s' | awk '{print $2}'").strip()
        #It would be nice to handle an existing installation but until ufw can easily
        #predefine rules in a conf we'll need to just mark it if woven installs it
        if not ufw:
            if env.verbosity:
                print env.host, "INSTALLING & ENABLING FIREWALL ufw"
            apt_get_install('ufw')
            set_server_state('ufw_installed')
        sudo('ufw allow %s/tcp'% env.port) #ssh port
        for rule in env.UFW_RULES:
            if rule:
                if env.verbosity:
                    print ' *',rule
                sudo('ufw '+rule)
        backup_file('/etc/ufw/ufw.conf')
        sed('/etc/ufw/ufw.conf','ENABLED=no','ENABLED=yes',use_sudo=True)
        sudo('ufw reload')
    else:
        #if it was installed by woven remove it else leave it the hell alone
        if server_state('ufw_installed'): 
            sudo('ufw disable')
            apt_get_purge('ufw')
            set_server_state('ufw_installed',delete=True)


def uncomment_sources(rollback=False):
    """
    Uncomments universe sources in /etc/apt/sources.list if necessary
    #(.?)deb(.*)http:(.*)universe
    """
    if not rollback:
        if contains(filename='/etc/apt/sources.list',text='#(.?)deb(.*)http:(.*)universe'):
            if env.verbosity:
                print env.host, "UNCOMMENTING universe SOURCES in /etc/apt/sources.list"
            backup_file('/etc/apt/sources.list')
            uncomment('/etc/apt/sources.list','#(.?)deb(.*)http:(.*)universe',use_sudo=True)
    else:
        restore_file('/etc/apt/sources.list')

def upgrade_ubuntu():
    """
    Update to latest packages 
    """
    if env.verbosity:
        print env.host, "apt-get UPDATING and UPGRADING SERVER PACKAGES"
        print " * running apt-get update "
    sudo('apt-get -qqy update')
    if env.verbosity:
        print " * running apt-get upgrade (note: this may take sometime to complete if the host has not been upgraded recently)"
    sudo('apt-get -qqy upgrade')

def upload_ssh_key(rollback=False):
    """
    Upload your ssh key for passwordless logins
    """
    auth_keys = '/home/%s/.ssh/authorized_keys'% env.user
    if not rollback:    
        if not exists('.ssh'):
            run('mkdir .ssh')
           
        #determine local .ssh dir
        home = os.path.expanduser('~')
    
        ssh_dsa = os.path.join(home,'.ssh/id_dsa.pub')
        ssh_rsa =  os.path.join(home,'.ssh/id_rsa.pub')
        if os.path.exists(ssh_dsa):
            ssh_key = ssh_dsa
        elif os.path.exists(ssh_rsa):
            ssh_key = ssh_rsa
        else:
            ssh_key = ''
    
        if ssh_key:
            ssh_file = open(ssh_key,'r').read()
            
            if exists(auth_keys):
                backup_file(auth_keys)
            if env.verbosity:
                print env.host, "UPLOADING SSH KEY if it doesn't already exist on host"
            append(ssh_file,auth_keys) #append prevents uploading twice
        return
    else:
        if exists(auth_keys+'.wovenbak'):
            restore_file('/home/%s/.ssh/authorized_keys'% env.user)
        else: #no pre-existing keys remove the .ssh directory
            sudo('rm -rf /home/%s/.ssh')
        return

    
    

