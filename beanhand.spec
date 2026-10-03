# See https://docs.fedoraproject.org/en-US/packaging-guidelines/Python/#_example_spec_file

%define debug_package %{nil}
%undefine _py3_shebang_s

%define package_name beanhand
%define module_name beanhand

%define mybuildnumber %{?build_number}%{?!build_number:1}

Name:           python-%{package_name}
Version:        0.8.1
Release:        %{mybuildnumber}%{?dist}
Summary:        AI-assisted CLI to help you work on your Beancount ledger

License:        BSD
URL:            https://github.com/Rudd-O/%{package_name}
Source:         %{module_name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  pyproject-rpm-macros, python3-devel, python3-setuptools, coreutils

%global _description %{expand:
A command-line computer program that assists you with frequent time-consuming tasks, like creating detailed transaction from a receipt, filing receipts with existing transactions, and adding details to transactions based on receipts.  It delegates drudgery like typing or reading receipts to an LLM.  Your data can stay 100% private, if you choose to.  It's open source, free software — you can install and use it on your desktop computer for free.}

%description %_description

%package -n python3-%{package_name}
Summary:        %{summary}
Requires:       xdg-utils
Obsoletes:      python3-beancount-ai < 0.7.2
Provides:       python3-beancount-ai = %{version}-%{release}

%description -n python3-%{package_name} %_description

%package -n python3-%{package_name}-qubes-rpc
Summary:        Provides Qubes services to invoke beanhand-documents-server and beanhand-ai-server from another Qubes OS VM
Requires:       qubes-core-qrexec
Requires:       python3-%{package_name} = %{version}-%{release}
Obsoletes:      python3-beancount-ai-qubes-rpc < 0.7.2
Provides:       python3-beancount-ai-qubes-rpc = %{version}-%{release}

%description -n python3-%{package_name}-qubes-rpc %{expand:
These are stub files to provide Qubes RPC services to VMs authorized to invoke
beanhand-documents-server and beanhand-ai-server.}

%prep
%autosetup -p1 -n %{module_name}-%{version}

%generate_buildrequires
%pyproject_buildrequires -t


%build
%pyproject_wheel


%install
%pyproject_install

mkdir -p %{buildroot}/etc/qubes-rpc
for rpc in qubes-rpc/* ; do
  install -m 755 -t %{buildroot}/etc/qubes-rpc "$rpc"
done
mkdir -p %{buildroot}/%{_bindir}
cd %{buildroot}/%{_bindir}
ln -sf %{package_name} bean-ai
ln -sf %{package_name} bh

%pyproject_save_files %{module_name}


%check
%{!?disable_tests:%{tox}}%{?disable_tests:true}


%files -n python3-%{package_name} -f %{pyproject_files}
%{_bindir}/%{package_name}
%{_bindir}/bh
%{_bindir}/%{package_name}-documents-server
%{_bindir}/%{package_name}-ai-server
%{_bindir}/bean-ai
%doc README.md docs/

%files -n python3-%{package_name}-qubes-rpc
%attr(0755, root, root) /etc/qubes-rpc/beanhand.*

%changelog
* Tue Sep 08 2026 Manuel Amador <rudd-o@rudd-o.com> 0.7.3
- Split the single beanhand-server program into beanhand-documents-server and
  beanhand-ai-server, which can now run on two separate VMs.
* Mon Sep 07 2026 Manuel Amador <rudd-o@rudd-o.com> 0.7.2
- Rename program from beancount-ai to beanhand.
* Sun Aug 16 2026 Manuel Amador <rudd-o@rudd-o.com> 0.1.0
- First RPM packaging release
